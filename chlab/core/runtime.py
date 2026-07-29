"""Bringing clusters up and down."""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable, Iterable

from . import compose, configgen, seed
from .cache import Cache
from .image import ensure_base_image
from .ports import allocate_block
from .query import QueryError, execute, keeper_ok, ping
from .registry import InstanceExists, Registry
from .resolve import Resolved, resolve
from .spec import ClusterSpec
from .topology import Node, Topology, build_topology, port_span

EventCallback = Callable[[str], None]

HEALTH_TIMEOUT = 180
HEALTH_INTERVAL = 1.0
LOG_TAIL_ON_FAILURE = 40
COMPOSE_TIMEOUT = 900


class RuntimeError_(RuntimeError):
    """A failure bringing a cluster up or down."""


class InitError(RuntimeError):
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
    services: tuple[ServiceStatus, ...]

    @property
    def running(self) -> bool:
        return bool(self.services) and all(service.running for service in self.services)

    @property
    def any_running(self) -> bool:
        return any(service.running for service in self.services)

    @property
    def state(self) -> str:
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
        on_event: EventCallback | None = None,
    ) -> InstanceStatus:
        """Resolve, generate, launch, wait, and initialise."""
        notify = on_event or (lambda _message: None)

        if self.registry.exists(spec.name) and not recreate:
            raise InstanceExists(
                f"instance {spec.name!r} already exists; "
                "use `chlab rm` to remove it or pick another --name"
            )

        resolved = resolve(
            spec.source,
            version=spec.version,
            force=force_resolve,
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

        notify("writing configuration")
        configgen.write_configs(topology, directory, base_image_mode=not resolved.runs_as_image)
        compose.write(topology, resolved, directory, image)

        notify(f"starting {len(topology.nodes)} node(s)")
        self._compose(spec, "up", "-d", "--remove-orphans")

        self._wait_healthy(topology, notify=notify)
        self._initialise(topology, notify=notify)

        return self.status(spec.name)

    def stop(self, name: str) -> None:
        self._compose(self.registry.load(name), "stop")

    def start(self, name: str) -> InstanceStatus:
        """Start an instance, recreating any containers that no longer exist.

        Uses ``up -d`` rather than ``start`` so an instance that has been through ``down`` —
        or whose containers were removed outside chlab — comes back on its existing data
        volumes instead of being stranded with no way forward but ``rm``.
        """
        spec = self.registry.load(name)
        self._compose(spec, "up", "-d", "--remove-orphans")
        self._wait_healthy(build_topology(spec))
        return self.status(name)

    def down(self, name: str) -> None:
        """Remove containers, keeping data volumes and the instance definition."""
        self._compose(self.registry.load(name), "down", "--remove-orphans")

    def destroy(self, name: str) -> None:
        """Remove containers, volumes and the instance definition."""
        spec = self.registry.load(name)
        self._compose(spec, "down", "--volumes", "--remove-orphans", check=False)
        self.registry.delete(name)

    # --- inspection ----------------------------------------------------------------------

    def status(self, name: str) -> InstanceStatus:
        spec = self.registry.load(name)
        return InstanceStatus(
            spec=spec,
            topology=build_topology(spec),
            services=tuple(self._services(spec)),
        )

    def list(self) -> list[InstanceStatus]:
        return [self.status(spec.name) for spec in self.registry.list()]

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
        status = self.status(name)
        target = status.topology.node(node) if node else status.topology.nodes[0]
        container = f"{status.spec.project_name}-{target.name}"
        return ["docker", "exec", "-it", container, "clickhouse", "client", "--host", "127.0.0.1"]

    def query(
        self,
        name: str,
        sql: str,
        *,
        node: str | None = None,
        user: str = "default",
        password: str = "",
        database: str | None = None,
    ):
        """Run a query against one node, or every node when ``node`` is None and fan-out is asked."""
        status = self.status(name)
        target = status.topology.node(node) if node else status.topology.nodes[0]
        return execute(
            target.http_url, sql, user=user, password=password, database=database, node=target.name
        )

    def query_all(
        self,
        name: str,
        sql: str,
        *,
        user: str = "default",
        password: str = "",
        database: str | None = None,
    ) -> list:
        """Run the same query on every node; failures come back as QueryError values."""
        status = self.status(name)
        results = []
        for node in status.topology.nodes:
            try:
                results.append(
                    execute(
                        node.http_url,
                        sql,
                        user=user,
                        password=password,
                        database=database,
                        node=node.name,
                    )
                )
            except QueryError as failure:
                results.append(failure)
        return results

    # --- internals -----------------------------------------------------------------------

    def _wait_healthy(self, topology: Topology, *, notify: EventCallback | None = None) -> None:
        notify = notify or (lambda _message: None)
        self._wait_keepers(topology, notify=notify)
        self._wait_nodes(topology, notify=notify)
        self._wait_zookeeper_sessions(topology, notify=notify)

    def _wait_keepers(self, topology: Topology, *, notify: EventCallback) -> None:
        """Keeper listens only once its Raft election settles."""
        if not topology.keepers:
            return

        notify("waiting for keeper")
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = list(topology.keepers)
        while pending and time.monotonic() < deadline:
            pending = [keeper for keeper in pending if not keeper_ok(keeper.client_port)]
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(keeper.name for keeper in pending)
            raise RuntimeError_(f"{names} did not become ready within {HEALTH_TIMEOUT}s")

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
            pending = [node for node in pending if not self._zookeeper_connected(node)]
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(node.name for node in pending)
            raise RuntimeError_(
                f"{names} did not connect to keeper within {HEALTH_TIMEOUT}s\n\n"
                + self._tail_logs(topology.spec, pending)
            )

    def _zookeeper_connected(self, node: Node) -> bool:
        try:
            execute(node.http_url, "SELECT name FROM system.zookeeper WHERE path = '/'", node=node.name)
        except QueryError:
            return False
        return True

    def _wait_nodes(self, topology: Topology, *, notify: EventCallback) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        pending = list(topology.nodes)

        while pending and time.monotonic() < deadline:
            still_pending = [node for node in pending if not ping(node.http_url)]
            if len(still_pending) != len(pending):
                ready = len(topology.nodes) - len(still_pending)
                notify(f"waiting for nodes ({ready}/{len(topology.nodes)} ready)")
            pending = still_pending
            if pending:
                time.sleep(HEALTH_INTERVAL)

        if pending:
            names = ", ".join(node.name for node in pending)
            raise RuntimeError_(
                f"{names} did not become healthy within {HEALTH_TIMEOUT}s\n\n"
                + self._tail_logs(topology.spec, pending)
            )

    def _initialise(self, topology: Topology, *, notify: EventCallback) -> None:
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

    def _services(self, spec: ClusterSpec) -> list[ServiceStatus]:
        compose_file = self.registry.instance_dir(spec.name) / "docker-compose.yml"
        if not compose_file.is_file():
            return []
        result = subprocess.run(
            self._compose_command(spec, "ps", "--all", "--format", "json"),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return []
        return [
            ServiceStatus(
                name=entry.get("Service", entry.get("Name", "?")),
                state=entry.get("State", "unknown"),
                running=entry.get("State") == "running",
            )
            for entry in _parse_compose_ps(result.stdout)
        ]

    def _tail_logs(self, spec: ClusterSpec, nodes: Iterable[Node]) -> str:
        chunks = []
        for node in nodes:
            result = subprocess.run(
                self._compose_command(spec, "logs", f"--tail={LOG_TAIL_ON_FAILURE}", node.name),
                capture_output=True,
                text=True,
            )
            chunks.append(f"--- {node.name} ---\n{result.stdout.strip() or '(no output)'}")
        return "\n\n".join(chunks)

    def _compose_command(self, spec: ClusterSpec, *args: str) -> list[str]:
        compose_file = self.registry.instance_dir(spec.name) / "docker-compose.yml"
        return [
            *compose_executable(),
            "--project-name", spec.project_name,
            "--file", str(compose_file),
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
            raise RuntimeError_(
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
    raise RuntimeError_("docker compose was not found; install Docker Compose v2")


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
