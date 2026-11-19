"""Bringing clusters up and down."""

from __future__ import annotations

import json
import subprocess
import time
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

from . import compose, configgen, seed
from . import services as companions
from .cache import Cache
from .errors import ChlabError
from .image import ensure_base_image
from .ports import allocate_block
from .query import QueryError, execute, keeper_ok, ping
from .registry import InstanceExists, Registry
from .resolve import resolve
from .spec import ClusterSpec
from .topology import Node, Topology, build_topology, port_span

EventCallback = Callable[[str], None]

HEALTH_TIMEOUT = 180
HEALTH_INTERVAL = 1.0
LOG_TAIL_ON_FAILURE = 40
COMPOSE_TIMEOUT = 900


#: Fan-out and readiness polling are IO-bound waits on independent nodes.
MAX_PARALLEL_NODES = 16


class ClusterError(ChlabError):
    """A failure bringing a cluster up or down."""


class InitError(ChlabError):
    """The cluster started but the post-start DDL failed."""


@dataclass(frozen=True)
class ServiceStatus:
    name: str
    state: str
    running: bool


@dataclass(frozen=True)
class InstanceStatus:
    spec: ClusterSpec
    topology: Topology
    #: None when Docker could not be reached, as opposed to () for "no containers exist".
    services: tuple[ServiceStatus, ...] | None

    @property
    def running(self) -> bool:
        return bool(self.services) and all(service.running for service in self.services)

    @property
    def any_running(self) -> bool:
        return any(service.running for service in self.services or ())

    @property
    def state(self) -> str:
        if self.services is None:
            # Reporting "created" here would claim the containers are gone when in fact we
            # simply could not ask.
            return "unknown"
        if not self.services:
            return "created"
        if self.running:
            return "running"
        return "partial" if self.any_running else "stopped"


class Runtime:
    def __init__(self, registry: Registry | None = None, cache: Cache | None = None):
        self.registry = registry or Registry()
        self.cache = cache or Cache.default()

    # --- lifecycle -----------------------------------------------------------------------

    def up(
        self,
        spec: ClusterSpec,
        *,
        force_resolve: bool = False,
        recreate: bool = False,
        sha256: str | None = None,
        trust_local: bool = True,
        on_event: EventCallback | None = None,
    ) -> InstanceStatus:
        """Resolve, generate, launch, wait, and initialise.

        ``trust_local=False`` is for a source someone else chose (see ``resolve``).
        """
        notify = on_event or (lambda _message: None)

        if self.registry.exists(spec.name):
            if not recreate:
                raise InstanceExists(
                    f"instance {spec.name!r} already exists. Pass --recreate to relaunch it in "
                    f"place (keeping its ports and data), `chlab rm {spec.name}` to delete it, "
                    "or --name to launch a second one alongside."
                )
            spec = self._inherit_ports(spec, notify)

        resolved = resolve(
            spec.source,
            version=spec.version,
            force=force_resolve,
            sha256=sha256,
            trust_local=trust_local,
            cache=self.cache,
            on_event=notify,
        )
        spec = spec.with_version(resolved.version)

        if spec.port_base is None:
            span = port_span(spec)
            base = allocate_block(span, taken=self.registry.taken_ports(excluding=spec.name))
            spec = spec.with_ports(base)
            notify(f"allocated ports {base}-{base + span - 1}")

        topology = build_topology(spec)
        directory = self.registry.save(spec)

        image = resolved.docker_image
        if image is None:
            notify("building the chlab base image")
            image = ensure_base_image()

        for service, _endpoint in companions.active(topology):
            service.prepare(topology, directory, self.cache, notify)

        notify("writing configuration")
        configgen.write_configs(topology, directory, base_image_mode=not resolved.runs_as_image)
        compose.write(topology, resolved, directory, image)

        notify(f"starting {len(topology.nodes)} node(s)")
        self._compose(spec, "up", "-d", "--remove-orphans")

        self._wait_healthy(topology, notify=notify)
        self._initialise(topology, notify=notify)

        return self.status(spec.name)

    def _inherit_ports(self, spec: ClusterSpec, notify: EventCallback) -> ClusterSpec:
        """Keep a recreated instance on the ports it already had.

        Without this, `--recreate` would allocate a fresh block, so every bookmarked `/play`
        URL and every open client would silently point at nothing — while the data volumes,
        which are keyed by project name rather than port, carried on being reused.
        """
        if spec.port_base is not None:
            return spec
        try:
            existing = self.registry.load(spec.name)
        except ChlabError:
            return spec  # unreadable spec: fall through to a fresh allocation
        if existing.port_base is None:
            return spec
        notify(f"reusing ports {existing.port_base}-{existing.port_base + port_span(spec) - 1}")
        return spec.with_ports(existing.port_base)

    def stop(self, name: str) -> None:
        self._compose(self.registry.load(name), "stop")

    def start(self, name: str, *, on_event: EventCallback | None = None) -> InstanceStatus:
        """Start an instance, recreating any containers that no longer exist.

        Uses ``up -d`` rather than ``start`` so an instance that has been through ``down`` —
        or whose containers were removed outside chlab — comes back on its existing data
        volumes instead of being stranded with no way forward but ``rm``.
        """
        notify = on_event or (lambda _message: None)
        spec = self.registry.load(name)
        notify("starting containers")
        self._compose(spec, "up", "-d", "--remove-orphans")
        self._wait_healthy(build_topology(spec), notify=notify)
        return self.status(name)

    def restart(
        self, name: str, services: Iterable[str] = (), *, on_event: EventCallback | None = None
    ) -> InstanceStatus:
        """Restart some or all services, then wait until the cluster is usable again.

        A restart is how a reproduction checks what survives a table being reopened, so this
        waits for the full readiness sequence rather than returning while servers boot.
        """
        notify = on_event or (lambda _message: None)
        spec = self.registry.load(name)
        topology = build_topology(spec)
        targets = [topology.service(service) for service in services]
        notify(f"restarting {', '.join(targets) or 'all services'}")
        self._compose(spec, "restart", *targets)
        self._wait_healthy(topology, notify=notify)
        return self.status(name)

    def down(self, name: str) -> None:
        """Remove containers, keeping data volumes and the instance definition."""
        self._compose(self.registry.load(name), "down", "--remove-orphans")

    def destroy(self, name: str) -> None:
        """Remove containers, volumes and the instance definition.

        The definition goes only once Docker has removed the rest: if compose fails, the
        containers and volumes may still be there, and without the definition chlab could no
        longer find or remove them. (Compose succeeds for a project that is already gone.)
        """
        spec = self.registry.load(name)
        try:
            self._compose(spec, "down", "--volumes", "--remove-orphans")
        except (ClusterError, OSError, subprocess.SubprocessError) as failure:
            raise ClusterError(
                f"could not remove {name}'s containers and volumes, so its definition is kept "
                f"for another try: {failure}\nTo forget it anyway, delete "
                f"{self.registry.instance_dir(name)}"
            ) from failure
        self.registry.delete(name)

    # --- inspection ----------------------------------------------------------------------

    def status(self, name: str) -> InstanceStatus:
        spec = self.registry.load(name)
        return self._status(spec, _compose_containers())

    def list(self) -> list[InstanceStatus]:
        """Every instance, using a single `docker ps` rather than one per instance."""
        containers = _compose_containers()
        return [self._status(spec, containers) for spec in self.registry.list()]

    def _status(
        self, spec: ClusterSpec, containers: dict[str, list[ServiceStatus]] | None
    ) -> InstanceStatus:
        services = None if containers is None else tuple(containers.get(spec.project_name, ()))
        return InstanceStatus(spec=spec, topology=build_topology(spec), services=services)

    def logs(
        self, name: str, service: str | None = None, *, follow: bool = False, tail: int = 200
    ) -> int:
        spec = self.registry.load(name)
        args = ["logs", f"--tail={tail}"]
        if follow:
            args.append("--follow")
        if service:
            args.append(service)
        # Streamed straight to the terminal rather than captured.
        return subprocess.call(self._compose_command(spec, *args))

    def client_command(self, name: str, node: str | None = None) -> list[str]:
        """The ``docker exec`` invocation that opens clickhouse-client on a node."""
        topology = build_topology(self.registry.load(name))
        target = topology.node(node) if node else topology.nodes[0]
        return [
            "docker",
            "exec",
            "-it",
            topology.container(target.name),
            "clickhouse",
            "client",
            "--host",
            "127.0.0.1",
        ]

    def exec_command(
        self,
        name: str,
        command: Iterable[str],
        *,
        service: str | None = None,
        interactive: bool = False,
        tty: bool = False,
    ) -> list[str]:
        """The ``docker exec`` invocation that runs ``command`` inside a service's container."""
        topology = build_topology(self.registry.load(name))
        flags = (["-i"] if interactive else []) + (["-t"] if tty else [])
        return ["docker", "exec", *flags, topology.container(service), *command]

    def copy_command(
        self,
        name: str,
        *,
        service: str | None,
        container_path: str,
        host_path: str,
        to_container: bool,
    ) -> list[str]:
        """The ``docker cp`` invocation that moves a file into or out of a service."""
        topology = build_topology(self.registry.load(name))
        remote = f"{topology.container(service)}:{container_path}"
        pair = [host_path, remote] if to_container else [remote, host_path]
        return ["docker", "cp", *pair]

    def client_script_command(
        self,
        name: str,
        *,
        node: str | None = None,
        user: str = "default",
        password: str = "",
        database: str | None = None,
        settings: dict[str, str] | None = None,
        output_format: str | None = None,
    ) -> list[str]:
        """``clickhouse client --multiquery`` inside a node, reading statements from stdin.

        Files of several statements go through the server's own client rather than the HTTP
        interface: HTTP runs exactly one statement per request, and splitting SQL on ``;``
        outside a real parser breaks on the first string literal that contains one.
        """
        topology = build_topology(self.registry.load(name))
        target = topology.node(node) if node else topology.nodes[0]
        command = [
            "docker",
            "exec",
            "-i",
            topology.container(target.name),
            "clickhouse",
            "client",
            "--host",
            "127.0.0.1",
            "--multiquery",
            "--user",
            user,
        ]
        if password:
            command += ["--password", password]
        if database:
            command += ["--database", database]
        if output_format:
            command += ["--format", output_format]
        for key, value in (settings or {}).items():
            command.append(f"--{key}={value}")
        return command

    def query(
        self,
        name: str,
        sql: str,
        *,
        node: str | None = None,
        user: str = "default",
        password: str = "",
        database: str | None = None,
        settings: dict[str, str] | None = None,
        query_id: str | None = None,
    ):
        """Run a query against a single node, defaulting to the first. See query_all for fan-out."""
        topology = build_topology(self.registry.load(name))
        target = topology.node(node) if node else topology.nodes[0]
        try:
            result = execute(
                target.http_url,
                sql,
                user=user,
                password=password,
                database=database,
                settings=settings,
                node=target.name,
                query_id=query_id,
            )
        except QueryError as failure:
            failure.query_id = query_id
            raise
        result.query_id = query_id
        return result

    def query_all(
        self,
        name: str,
        sql: str,
        *,
        user: str = "default",
        password: str = "",
        database: str | None = None,
        settings: dict[str, str] | None = None,
        query_id: str | None = None,
    ) -> list:
        """Run the same query on every node; failures come back as QueryError values.

        With ``query_id``, each node's query gets ``<query_id>-<node>``: query IDs are unique
        per server, and these are separate queries.

        Nodes are queried concurrently. Serially, a query that times out everywhere would take
        the timeout multiplied by the node count — half an hour at the 30-node maximum — which
        defeats the point of a fan-out.
        """
        topology = build_topology(self.registry.load(name))

        def run(node: Node):
            node_query_id = f"{query_id}-{node.name}" if query_id else None
            try:
                result = execute(
                    node.http_url,
                    sql,
                    user=user,
                    password=password,
                    database=database,
                    settings=settings,
                    node=node.name,
                    query_id=node_query_id,
                )
            except QueryError as failure:
                failure.query_id = node_query_id
                return failure
            result.query_id = node_query_id
            return result

        return _in_parallel(run, topology.nodes)

    def profile_events(self, name: str, query_id: str, pattern: str = "") -> dict[str, int]:
        """ProfileEvents of a finished query, summed over every node that took part in it.

        A query on a Distributed table, or through ``s3Cluster``, does part of its work on
        other nodes under the same ``initial_query_id`` — so "how many S3 GETs did this
        query make" has to be asked of the whole cluster. ``pattern`` is a regular expression
        over event names (``S3|ReadBuffer``); empty means all non-zero events.
        """
        topology = build_topology(self.registry.load(name))
        # Each server buffers its query log; flush them all, not just the one we read from.
        _in_parallel(lambda node: execute(node.http_url, "SYSTEM FLUSH LOGS"), topology.nodes)
        sql = (
            "SELECT event, sum(value) FROM clusterAllReplicas("
            f"{seed.quote(topology.spec.cluster_name)}, system.query_log) "
            "ARRAY JOIN mapKeys(ProfileEvents) AS event, mapValues(ProfileEvents) AS value "
            f"WHERE initial_query_id = {seed.quote(query_id)} AND type != 'QueryStart' "
            f"AND match(event, {seed.quote(pattern)}) AND value != 0 "
            "GROUP BY event ORDER BY event"
        )
        result = execute(topology.nodes[0].http_url, sql)
        return {row[0]: int(row[1]) for row in result.rows}

    # --- internals -----------------------------------------------------------------------

    def _wait_healthy(self, topology: Topology, *, notify: EventCallback | None = None) -> None:
        notify = notify or (lambda _message: None)
        self._wait_services(topology, notify=notify)
        self._wait_keepers(topology, notify=notify)
        self._wait_nodes(topology, notify=notify)
        self._wait_zookeeper_sessions(topology, notify=notify)

    def _wait_services(self, topology: Topology, *, notify: EventCallback) -> None:
        active = list(companions.active(topology))
        if not active:
            return
        notify(f"waiting for {', '.join(service.name for service, _ in active)}")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = active
        while pending and time.monotonic() < deadline:
            ready = _in_parallel(lambda item: item[0].is_ready(topology, item[1]), pending)
            pending = [item for item, ok in zip(pending, ready, strict=True) if not ok]
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(service.name for service, _ in pending)
            raise ClusterError(
                f"{names} did not become ready within {HEALTH_TIMEOUT}s\n\n"
                + self._tail_service_logs(topology.spec, [service.name for service, _ in pending])
            )

    def _wait_keepers(self, topology: Topology, *, notify: EventCallback) -> None:
        """Keeper listens only once its Raft election settles."""
        if not topology.keepers:
            return

        notify("waiting for keeper")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = list(topology.keepers)
        while pending and time.monotonic() < deadline:
            ready = _in_parallel(lambda k: keeper_ok(k.client_port), pending)
            pending = [keeper for keeper, ok in zip(pending, ready, strict=True) if not ok]
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(keeper.name for keeper in pending)
            raise ClusterError(f"{names} did not become ready within {HEALTH_TIMEOUT}s")

    def _wait_zookeeper_sessions(self, topology: Topology, *, notify: EventCallback) -> None:
        """A healthy server is not necessarily connected to Keeper yet.

        Replicated DDL fails outright rather than retrying, so the session has to exist before
        any table is created.
        """
        if not topology.keepers:
            return

        notify("waiting for keeper sessions")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = list(topology.nodes)
        while pending and time.monotonic() < deadline:
            connected = _in_parallel(self._zookeeper_connected, pending)
            pending = [node for node, ok in zip(pending, connected, strict=True) if not ok]
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(node.name for node in pending)
            raise ClusterError(
                f"{names} did not connect to keeper within {HEALTH_TIMEOUT}s\n\n"
                + self._tail_logs(topology.spec, pending)
            )

    def _zookeeper_connected(self, node: Node) -> bool:
        try:
            execute(
                node.http_url, "SELECT name FROM system.zookeeper WHERE path = '/'", node=node.name
            )
        except QueryError:
            return False
        return True

    def _wait_nodes(self, topology: Topology, *, notify: EventCallback) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = list(topology.nodes)

        while pending and time.monotonic() < deadline:
            reachable = _in_parallel(lambda node: ping(node.http_url), pending)
            still_pending = [node for node, ok in zip(pending, reachable, strict=True) if not ok]
            if len(still_pending) != len(pending):
                ready = len(topology.nodes) - len(still_pending)
                notify(f"waiting for nodes ({ready}/{len(topology.nodes)} ready)")
            pending = still_pending
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(node.name for node in pending)
            raise ClusterError(
                f"{names} did not become healthy within {HEALTH_TIMEOUT}s\n\n"
                + self._tail_logs(topology.spec, pending)
            )

    def _initialise(self, topology: Topology, *, notify: EventCallback) -> None:
        for service, endpoint in companions.active(topology):
            try:
                service.initialise(topology, endpoint, notify)
            except ChlabError as failure:
                raise InitError(
                    f"the cluster is running, but setting up {service.name} failed:\n{failure}"
                ) from failure

        plan = seed.build_plan(topology)
        if not plan.common and not plan.has_seed:
            return

        notify("creating users and databases")
        for node in topology.nodes:
            self._run_statements(node, plan.common)

        if not plan.has_seed:
            return

        notify("creating demo tables")
        for node in topology.nodes:
            self._run_statements(node, plan.seed_ddl)

        for node in topology.nodes:
            statement = plan.seed_inserts.get(node.name)
            if statement and self._table_is_empty(node):
                notify(f"seeding {node.name}")
                self._run_statements(node, [statement])

    def _run_statements(self, node: Node, statements: Iterable[str]) -> None:
        for statement in statements:
            try:
                execute(node.http_url, statement, node=node.name)
            except QueryError as failure:
                raise InitError(
                    f"the cluster is running, but initialisation failed on {node.name}:\n"
                    f"{statement}\n\n{failure}"
                ) from failure

    def _table_is_empty(self, node: Node) -> bool:
        try:
            result = execute(
                node.http_url,
                f"SELECT count() FROM {seed.DEMO_DATABASE}.{seed.DEMO_TABLE}",
                node=node.name,
            )
        except QueryError:
            return True
        return not result.rows or int(result.rows[0][0]) == 0

    def _tail_logs(self, spec: ClusterSpec, nodes: Iterable[Node]) -> str:
        return self._tail_service_logs(spec, [node.name for node in nodes])

    def _tail_service_logs(self, spec: ClusterSpec, services: Iterable[str]) -> str:
        chunks = []
        for service in services:
            result = subprocess.run(
                self._compose_command(spec, "logs", f"--tail={LOG_TAIL_ON_FAILURE}", service),
                capture_output=True,
                text=True,
            )
            chunks.append(f"--- {service} ---\n{result.stdout.strip() or '(no output)'}")
        return "\n\n".join(chunks)

    def _compose_command(self, spec: ClusterSpec, *args: str) -> list[str]:
        compose_file = self.registry.instance_dir(spec.name) / "docker-compose.yml"
        return [
            *compose_executable(),
            "--project-name",
            spec.project_name,
            "--file",
            str(compose_file),
            *args,
        ]

    def _compose(self, spec: ClusterSpec, *args: str, check: bool = True) -> str:
        result = subprocess.run(
            self._compose_command(spec, *args),
            capture_output=True,
            text=True,
            timeout=COMPOSE_TIMEOUT,
        )
        if check and result.returncode != 0:
            raise ClusterError(
                "docker compose failed:\n" + (result.stderr.strip() or result.stdout.strip())
            )
        return result.stdout


@lru_cache(maxsize=1)
def compose_executable() -> tuple[str, ...]:
    """``docker compose`` where available, falling back to standalone ``docker-compose``."""
    for candidate in (("docker", "compose"), ("docker-compose",)):
        try:
            result = subprocess.run(
                [*candidate, "version"], capture_output=True, text=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0:
            return candidate
    raise ClusterError("docker compose was not found; install Docker Compose v2")


def _in_parallel(work, items: list):
    """Map ``work`` over ``items`` concurrently, preserving order."""
    items = list(items)
    if len(items) < 2:
        return [work(item) for item in items]
    with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_NODES, len(items))) as pool:
        return list(pool.map(work, items))


def _compose_containers() -> dict[str, list[ServiceStatus]] | None:
    """Every compose-managed container on the host, bucketed by project.

    One `docker ps` for all instances rather than one `compose ps` each: process spawns cost
    a few hundred milliseconds apiece, and `chlab ls` and the web dashboard both iterate every
    instance. Returns None when Docker cannot be reached, which the caller reports as
    "unknown" rather than pretending the containers are gone.
    """
    result = subprocess.run(
        ["docker", "ps", "--all", "--no-trunc", "--format", "json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    buckets: dict[str, list[ServiceStatus]] = defaultdict(list)
    for entry in _parse_compose_ps(result.stdout):
        labels = _parse_labels(entry.get("Labels", ""))
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if not project or not service:
            continue
        state = entry.get("State", "unknown")
        buckets[project].append(
            ServiceStatus(name=service, state=state, running=state == "running")
        )
    return dict(buckets)


def _parse_labels(raw: str) -> dict[str, str]:
    labels = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        if separator:
            labels[key.strip()] = value.strip()
    return labels


def _parse_compose_ps(output: str) -> list[dict]:
    """Compose has emitted both a JSON array and newline-delimited objects; accept either."""
    text = output.strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        entries = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    return payload if isinstance(payload, list) else [payload]
