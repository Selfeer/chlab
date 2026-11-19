"""chlab command line."""

from __future__ import annotations

import hashlib
import json as jsonlib
import os
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from rich.console import Console
from rich.markup import escape
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TransferSpeedColumn,
)
from rich.table import Table

from .core import profiles as profiles_module
from .core.cache import Cache
from .core.errors import ChlabError
from .core.query import QueryError, QueryResult
from .core.registry import Registry
from .core.resolve import Resolved, resolve
from .core.runtime import InstanceStatus, Runtime
from .core.spec import (
    DEFAULT_BUCKET,
    ClusterSpec,
    SpecError,
    parse_database,
    parse_grant,
    parse_s3_user,
    parse_setting,
    parse_user,
    suggest_name,
)

app = typer.Typer(
    help="Run any ClickHouse build, immediately.",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(help="Inspect and clear the artifact cache.", no_args_is_help=True)
s3_app = typer.Typer(
    help="Work with an instance's s3 service (started with --with s3). Paths are "
    "BUCKET/KEY. Every command runs mc in a throwaway container and exits with its code.",
    no_args_is_help=True,
)
profiles_app = typer.Typer(help="Saved cluster topologies.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")
app.add_typer(profiles_app, name="profiles")
app.add_typer(s3_app, name="s3")
repro_app = typer.Typer(
    help="Reproductions as directories: repro.yaml describes the cluster, numbered "
    ".sql/.sh/.py/.chlab files are the steps. See `chlab repro init`.",
    no_args_is_help=True,
)
app.add_typer(repro_app, name="repro")

console = Console()
error_console = Console(stderr=True)

JsonOption = Annotated[bool, typer.Option("--json", help="Emit JSON instead of a table.")]
NameArgument = Annotated[str, typer.Argument(metavar="NAME", help="Instance name.")]


# --- resolve / cache ---------------------------------------------------------------------


@app.command("resolve")
def resolve_command(
    source: Annotated[
        str,
        typer.Argument(
            metavar="SOURCE",
            help="docker:// image, http(s) URL, .deb/.tgz package, or path to a binary.",
        ),
    ],
    version: Annotated[
        str | None,
        typer.Option("--clickhouse-version", help="Skip detection and use this version."),
    ] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Ignore the cache.")] = False,
    sha256: Annotated[
        str | None, typer.Option("--sha256", help="Verify the download against this checksum.")
    ] = None,
    json: JsonOption = False,
) -> None:
    """Resolve a ClickHouse source to a runnable image or binary."""
    with _download_progress(disabled=json) as report:
        try:
            resolved = resolve(
                source,
                version=version,
                force=force,
                sha256=sha256,
                on_event=report.event,
                on_progress=report.progress,
            )
        except ChlabError as failure:
            _fail(failure)

    if json:
        console.print_json(jsonlib.dumps(_resolved_as_dict(resolved)))
        return

    table = _kv_table()
    table.add_row("source", resolved.source.raw)
    table.add_row("version", resolved.version or "unknown")
    if resolved.docker_image:
        table.add_row("image", resolved.docker_image)
    if resolved.binary_path:
        table.add_row("binary", str(resolved.binary_path))
    table.add_row("cached", "yes" if resolved.cached else "no")
    console.print(table)


@cache_app.command("path")
def cache_path() -> None:
    """Print the cache directory."""
    console.print(str(Cache.default().root))


@cache_app.command("ls")
def cache_ls(json: JsonOption = False) -> None:
    """List everything resolved so far."""
    cache = Cache.default()
    entries = []
    for meta_file in sorted((cache.root / "bin").glob("*/meta.json")):
        try:
            entries.append(jsonlib.loads(meta_file.read_text()))
        except (OSError, jsonlib.JSONDecodeError):
            continue

    if json:
        console.print_json(jsonlib.dumps(entries))
        return
    if not entries:
        console.print("[dim]nothing resolved yet[/dim]")
        return

    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("VERSION", style="bold")
    table.add_column("KIND")
    table.add_column("SIZE", justify="right")
    table.add_column("SOURCE", overflow="fold")
    for entry in sorted(entries, key=lambda item: item.get("version") or ""):
        table.add_row(
            entry.get("version") or "unknown",
            entry.get("kind", ""),
            _human_size(_entry_size(cache, entry)),
            entry.get("source", ""),
        )
    console.print(table)
    console.print(f"[dim]{cache.root}[/dim]")


@cache_app.command("rm")
def cache_rm(
    which: Annotated[str, typer.Argument(help="Version or source substring to remove.")],
) -> None:
    """Remove one cached artifact, so a bad download can be re-fetched on its own."""
    cache = Cache.default()
    removed = []
    for meta_file in sorted((cache.root / "bin").glob("*/meta.json")):
        try:
            entry = jsonlib.loads(meta_file.read_text())
        except (OSError, jsonlib.JSONDecodeError):
            continue
        if which not in (entry.get("version") or "") and which not in entry.get("source", ""):
            continue
        key = meta_file.parent.name
        for directory in (cache.bin_dir(key), cache.artifact_dir(key)):
            shutil.rmtree(directory, ignore_errors=True)
        removed.append(entry.get("version") or key)

    if not removed:
        _fail(SpecError(f"nothing cached matches {which!r}; see `chlab cache ls`"))
    console.print(f"removed {', '.join(removed)}")


@cache_app.command("clear")
def cache_clear(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete all downloaded artifacts and extracted binaries."""
    root = Cache.default().root
    if not root.exists():
        console.print("[dim]cache is already empty[/dim]")
        return
    if not yes:
        typer.confirm(f"Delete {root}?", abort=True)
    shutil.rmtree(root)
    console.print(f"removed {root}")


# --- lifecycle ---------------------------------------------------------------------------


@app.command("up")
def up_command(
    clickhouse: Annotated[
        str,
        typer.Option(
            "--clickhouse",
            "-c",
            help="docker:// image, http(s) URL, .deb/.tgz package, or path to a binary.",
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Instance name. Derived from the version if omitted."),
    ] = None,
    version: Annotated[
        str | None,
        typer.Option("--clickhouse-version", help="Skip detection and use this version."),
    ] = None,
    shards: Annotated[int | None, typer.Option("--shards", "-s", help="Number of shards.")] = None,
    replicas: Annotated[
        int | None, typer.Option("--replicas", "-r", help="Replicas per shard.")
    ] = None,
    keeper: Annotated[
        int | None,
        typer.Option(
            "--keeper", "-k", help="Keeper nodes. Defaults to 1 when replicating, else 0."
        ),
    ] = None,
    user: Annotated[
        list[str] | None,
        typer.Option(
            "--user",
            "-u",
            help="Create a user: name[:password][:none|readonly|readwrite|admin]. "
            "Defaults to readwrite. Repeatable.",
        ),
    ] = None,
    grant: Annotated[
        list[str] | None,
        typer.Option("--grant", help="Extra grant: user:PRIVILEGES ON target. Repeatable."),
    ] = None,
    database: Annotated[
        list[str] | None,
        typer.Option(
            "--database", "-d", help="Create a database: name[:Atomic|Replicated]. Repeatable."
        ),
    ] = None,
    setting: Annotated[
        list[str] | None,
        typer.Option("--setting", help="Server setting (config.d) as key=value. Repeatable."),
    ] = None,
    query_setting: Annotated[
        list[str] | None,
        typer.Option(
            "--query-setting",
            help="Query setting for every session, written into the default profile, as "
            "key=value — e.g. allow_insert_into_iceberg=1. Repeatable.",
        ),
    ] = None,
    with_service: Annotated[
        list[str] | None,
        typer.Option(
            "--with",
            "-w",
            help="Start a companion service next to the cluster: s3 (RustFS object storage), "
            "iceberg-rest (Apache REST catalog), ice-rest (Altinity ice-rest-catalog). The "
            "catalogs need s3. Repeatable.",
        ),
    ] = None,
    bucket: Annotated[
        list[str] | None,
        typer.Option(
            "--bucket",
            help=f"Bucket to create in the s3 service (default: {DEFAULT_BUCKET}). Repeatable.",
        ),
    ] = None,
    s3_user: Annotated[
        list[str] | None,
        typer.Option(
            "--s3-user",
            help="Extra S3 identity: name:secret[:readwrite|readonly|writeonly[:bucket,bucket]]. "
            "Secrets need 8+ characters. Repeatable.",
        ),
    ] = None,
    seed: Annotated[
        bool | None,
        typer.Option(
            "--seed/--no-seed",
            help="Create the demo database with generated rows. --no-seed overrides a profile.",
        ),
    ] = None,
    cluster_name: Annotated[
        str | None, typer.Option("--cluster-name", help="Name of the generated cluster.")
    ] = None,
    profile: Annotated[
        str | None, typer.Option("--profile", "-p", help="Start from a saved profile.")
    ] = None,
    force_resolve: Annotated[
        bool, typer.Option("--force-resolve", help="Re-download the source, ignoring the cache.")
    ] = False,
    sha256: Annotated[
        str | None, typer.Option("--sha256", help="Verify the download against this checksum.")
    ] = None,
    recreate: Annotated[
        bool,
        typer.Option("--recreate", help="Reuse an existing instance's name, ports and volumes."),
    ] = False,
) -> None:
    """Launch a ClickHouse cluster."""
    runtime = Runtime()
    registry = runtime.registry

    try:
        overrides: dict = {
            "shards": shards,
            "replicas": replicas,
            "keeper_nodes": keeper,
            "cluster_name": cluster_name,
            "seed": seed,
        }
        if user:
            overrides["users"] = _users_with_grants(user, grant or [])
        elif grant:
            raise SpecError("--grant needs a matching --user")
        if database:
            overrides["databases"] = tuple(parse_database(text) for text in database)
        if setting:
            overrides["settings"] = dict(parse_setting(text) for text in setting)
        if query_setting:
            overrides["profile_settings"] = dict(parse_setting(text) for text in query_setting)
        if with_service:
            overrides["services"] = tuple(dict.fromkeys(with_service))
        if bucket:
            overrides["buckets"] = tuple(bucket)
        if s3_user:
            overrides["s3_users"] = tuple(parse_s3_user(text) for text in s3_user)

        base = profiles_module.Profiles.default().get(profile) if profile else {}
        fields = profiles_module.apply(base, overrides)
        taken = set() if recreate else {spec.name for spec in registry.list()}
        instance_name = name or suggest_name(clickhouse, taken=taken)
        spec = ClusterSpec(name=instance_name, source=clickhouse, version=version, **fields)
    except ChlabError as failure:
        _fail(failure)

    with _download_progress() as report:
        try:
            status = runtime.up(
                spec,
                force_resolve=force_resolve,
                recreate=recreate,
                sha256=sha256,
                on_event=report.event,
            )
        except ChlabError as failure:
            _fail(failure)

    console.print()
    _print_instance(status)


@app.command("ls")
def ls_command(json: JsonOption = False) -> None:
    """List instances."""
    runtime = Runtime()
    statuses = runtime.list()

    if json:
        console.print_json(jsonlib.dumps([_status_as_dict(status) for status in statuses]))
        return
    if not statuses:
        console.print("[dim]no instances; create one with `chlab up --clickhouse <source>`[/dim]")
        return

    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("NAME", style="bold")
    table.add_column("STATE")
    table.add_column("VERSION")
    table.add_column("TOPOLOGY")
    table.add_column("HTTP")
    for status in statuses:
        first = status.topology.nodes[0]
        table.add_row(
            status.spec.name,
            _state_markup(status.state),
            status.spec.version or "unknown",
            _topology_summary(status),
            first.http_url,
        )
    console.print(table)


@app.command("status")
def status_command(name: NameArgument, json: JsonOption = False) -> None:
    """Show one instance in detail."""
    try:
        status = Runtime().status(name)
    except ChlabError as failure:
        _fail(failure)

    if json:
        console.print_json(jsonlib.dumps(_status_as_dict(status)))
        return
    _print_instance(status)


@app.command("stop")
def stop_command(name: NameArgument) -> None:
    """Stop an instance, keeping its containers and data."""
    _lifecycle(lambda runtime: runtime.stop(name), f"stopped {name}")


@app.command("start")
def start_command(name: NameArgument) -> None:
    """Start a stopped instance."""
    runtime = Runtime()
    with _spinner("starting") as report:
        try:
            status = runtime.start(name, on_event=report.event)
        except ChlabError as failure:
            _fail(failure)
    console.print()
    _print_instance(status)


@app.command("down")
def down_command(name: NameArgument) -> None:
    """Remove an instance's containers, keeping its data and definition."""
    _lifecycle(lambda runtime: runtime.down(name), f"removed containers for {name}")


@app.command("rm")
def rm_command(
    name: NameArgument,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete an instance entirely, including its data volumes."""
    if not yes:
        typer.confirm(f"Delete instance {name!r} and all of its data?", abort=True)
    _lifecycle(lambda runtime: runtime.destroy(name), f"deleted {name}")


@app.command("logs")
def logs_command(
    name: NameArgument,
    service: Annotated[
        str | None, typer.Argument(help="Node or keeper name. Defaults to all services.")
    ] = None,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new output.")] = False,
    tail: Annotated[int, typer.Option("--tail", help="Lines of history to show.")] = 200,
) -> None:
    """Show server logs."""
    runtime = Runtime()
    try:
        code = runtime.logs(name, service, follow=follow, tail=tail)
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=code)


# --- querying ----------------------------------------------------------------------------


@app.command("query")
def query_command(
    name: NameArgument,
    sql: Annotated[
        str | None, typer.Argument(help="SQL to run: one statement. Use --file for several.")
    ] = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file",
            "-f",
            help="Run every statement in this file ('-' for stdin) through clickhouse-client "
            "inside the node. Output is the client's own; stops at the first error.",
        ),
    ] = None,
    node: Annotated[str | None, typer.Option("--node", help="Run on this node.")] = None,
    all_nodes: Annotated[
        bool, typer.Option("--all", "-a", help="Run on every node and compare the results.")
    ] = False,
    setting: Annotated[
        list[str] | None,
        typer.Option("--setting", "-s", help="Query setting as key=value. Repeatable."),
    ] = None,
    user: Annotated[str, typer.Option("--user", "-u", help="ClickHouse user.")] = "default",
    password: Annotated[str, typer.Option("--password", help="ClickHouse password.")] = "",
    database: Annotated[str | None, typer.Option("--database", "-d")] = None,
    output_format: Annotated[
        str | None,
        typer.Option("--format", help="With --file: the client's output format, e.g. TSV."),
    ] = None,
    events: Annotated[
        str | None,
        typer.Option(
            "--events",
            "-e",
            metavar="REGEX",
            help="Also report the query's ProfileEvents whose names match REGEX, summed over "
            "every node that took part — e.g. 'S3GetObject|ReadBufferFromS3Bytes'. "
            "'.' shows all.",
        ),
    ] = None,
    json: JsonOption = False,
) -> None:
    """Run SQL against an instance.

    Exits 1 if the query fails on any node. With --json, failures are reported in the
    output as {"node", "error", "code"}, where code is ClickHouse's error code.
    """
    try:
        settings = dict(parse_setting(text) for text in setting or [])
        if (sql is None) == (file is None):
            raise SpecError("give exactly one of SQL or --file")
    except ChlabError as failure:
        _fail(failure)

    runtime = Runtime()
    if file is not None:
        if json:
            _fail(SpecError("--json is not supported with --file; use --format JSONEachRow"))
        if events is not None:
            _fail(SpecError("--events is not supported with --file; run the statement alone"))
        _run_sql_file(
            runtime,
            name,
            file,
            node=node,
            all_nodes=all_nodes,
            user=user,
            password=password,
            database=database,
            settings=settings,
            output_format=output_format,
        )
        return

    options = {"user": user, "password": password, "database": database, "settings": settings}
    if events is not None:
        import uuid

        options["query_id"] = f"chlab-{uuid.uuid4()}"
    try:
        if all_nodes:
            results = runtime.query_all(name, sql, **options)
        else:
            try:
                results = [runtime.query(name, sql, node=node, **options)]
            except QueryError as failure:
                results = [failure]
        if events is not None:
            for result in results:
                if result.query_id:
                    result.events = runtime.profile_events(name, result.query_id, events)
    except ChlabError as failure:
        _fail(failure)

    failed = any(isinstance(result, QueryError) for result in results)
    if json:
        console.print_json(jsonlib.dumps([_result_as_dict(result) for result in results]))
        raise typer.Exit(code=1 if failed else 0)

    for result in results:
        if isinstance(result, QueryError):
            label = f"{result.node}: " if result.node else ""
            error_console.print(f"[bold red]{escape(label)}error:[/bold red] {escape(str(result))}")
        else:
            if all_nodes:
                console.print(f"[bold]{result.node}[/bold]")
            _print_result(result)
        if result.events is not None:
            _print_events(result.events)

    if all_nodes:
        console.print(_agreement_line(results))
    raise typer.Exit(code=1 if failed else 0)


def _run_sql_file(runtime: Runtime, name: str, file: str, *, node, all_nodes, **options) -> None:
    import subprocess
    import sys

    try:
        script = sys.stdin.read() if file == "-" else Path(file).read_text()
    except OSError as failure:
        _fail(SpecError(f"could not read {file}: {failure.strerror or failure}"))

    try:
        nodes = list(runtime.status(name).topology.node_names) if all_nodes else [node]
        commands = [
            (target, runtime.client_script_command(name, node=target, **options))
            for target in nodes
        ]
    except ChlabError as failure:
        _fail(failure)

    worst = 0
    for target, command in commands:
        if all_nodes:
            console.print(f"[bold]{target}[/bold]")
        # Streamed, not captured: the client's output is the result.
        code = subprocess.run(command, input=script, text=True).returncode
        worst = worst or code
    raise typer.Exit(code=worst)


@app.command("client")
def client_command(
    name: NameArgument,
    node: Annotated[
        str | None, typer.Argument(help="Node to connect to. Defaults to the first.")
    ] = None,
) -> None:
    """Open an interactive clickhouse-client session on a node."""
    runtime = Runtime()
    try:
        command = runtime.client_command(name, node)
    except ChlabError as failure:
        _fail(failure)
    # Hand the terminal over so the REPL behaves exactly as it would run directly.
    os.execvp(command[0], command)


@app.command("exec")
def exec_command(
    name: NameArgument,
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Command to run. Put it after `--`. Defaults to bash."),
    ] = None,
    node: Annotated[
        str | None,
        typer.Option("--node", help="Node or keeper to run in. Defaults to the first node."),
    ] = None,
) -> None:
    """Run a command inside a node's container, e.g. `chlab exec lab -- ls /var/lib/clickhouse`.

    Exits with the command's own exit code. Stdin is passed through, and a terminal is
    allocated only when there is one, so this works the same from scripts and by hand.
    """
    import subprocess
    import sys

    interactive_terminal = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        docker_command = Runtime().exec_command(
            name,
            command or ["bash"],
            service=node,
            interactive=True,
            tty=interactive_terminal,
        )
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=subprocess.call(docker_command))


@app.command("cp")
def cp_command(
    source: Annotated[str, typer.Argument(help="NAME[/NODE]:PATH or a local path.")],
    destination: Annotated[str, typer.Argument(help="NAME[/NODE]:PATH or a local path.")],
) -> None:
    """Copy files into or out of a node, like `docker cp`.

    One side names the instance: `lab:/var/lib/clickhouse/user_files/x.parquet` is the first
    node, `lab/clickhouse2:/tmp` a specific one. Directories are copied recursively.
    """
    import subprocess

    runtime = Runtime()
    try:
        remote_source = _remote_path(source, runtime.registry)
        remote_destination = _remote_path(destination, runtime.registry)
        if (remote_source is None) == (remote_destination is None):
            raise SpecError(
                "exactly one side of `chlab cp` must be NAME[/NODE]:PATH, the other a local path"
            )
        to_container = remote_destination is not None
        instance, node, container_path = remote_destination or remote_source
        docker_command = runtime.copy_command(
            instance,
            service=node,
            container_path=container_path,
            host_path=source if to_container else destination,
            to_container=to_container,
        )
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=subprocess.call(docker_command))


def _remote_path(text: str, registry: Registry) -> tuple[str, str | None, str] | None:
    """Parse NAME[/NODE]:PATH, or return None for a local path."""
    prefix, separator, path = text.partition(":")
    if not separator:
        return None
    instance, _, node = prefix.partition("/")
    if not registry.exists(instance):
        # A local path that happens to contain a colon, unless it looks like ours.
        if "/" in instance or not instance:
            return None
        raise SpecError(f"no instance named {instance!r} (in {text!r})")
    return instance, node or None, path or "/"


@app.command("restart")
def restart_command(
    name: NameArgument,
    node: Annotated[
        list[str] | None,
        typer.Option("--node", help="Restart only this node or keeper. Repeatable."),
    ] = None,
) -> None:
    """Restart an instance's services and wait until they are ready again."""
    runtime = Runtime()
    with _spinner("restarting") as report:
        try:
            status = runtime.restart(name, node or (), on_event=report.event)
        except ChlabError as failure:
            _fail(failure)
    console.print(f"restarted {', '.join(node) if node else name}: {status.state}")


@app.command("env")
def env_command(name: NameArgument, json: JsonOption = False) -> None:
    """Endpoints, containers and credentials of an instance, from the host and the network."""
    from .core.environment import describe

    runtime = Runtime()
    try:
        info = describe(runtime.status(name), runtime.registry)
    except ChlabError as failure:
        _fail(failure)

    if json:
        console.print_json(jsonlib.dumps(info))
        return

    header = _kv_table()
    for key in ("name", "state", "version", "cluster", "compose_project", "network", "directory"):
        header.add_row(key, str(info[key] or "unknown"))
    console.print(header)
    console.print()

    table = Table(box=None, padding=(0, 2, 0, 0))
    for column in ("SERVICE", "CONTAINER", "FROM HOST", "IN NETWORK"):
        table.add_column(column, overflow="fold")
    for node in info["nodes"]:
        table.add_row(
            node["name"], node["container"], node["host"]["http"], node["network"]["http"]
        )
        table.add_row("", "", node["host"]["native"], node["network"]["native"])
    for keeper in info["keepers"]:
        table.add_row(keeper["name"], keeper["container"], keeper["host"], keeper["network"])
    for service_name, service in info["services"].items():
        table.add_row(
            service_name,
            service["container"],
            service["host"]["endpoint"],
            service["network"].get("clickhouse") or service["network"]["endpoint"],
        )
    console.print(table)
    console.print()

    for service_name, service in info["services"].items():
        snippets = _kv_table()
        for label, sql in service.get("sql", {}).items():
            snippets.add_row(f"{service_name} {label}", sql)
        console.print(snippets)
    if info["services"]:
        console.print()

    users = _kv_table()
    for user in info["users"]:
        users.add_row(user["name"], f"password={user['password']!r} role={user['role']}")
    console.print(users)


# --- tools container ---------------------------------------------------------------------


@app.command("py", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def py_command(
    ctx: typer.Context,
    name: NameArgument,
    script: Annotated[str, typer.Argument(help="Python script to run, or '-' for stdin.")],
) -> None:
    """Run a Python script in the tools container, wired to the instance.

    pyiceberg, pyarrow and boto3 are installed; catalogs, S3 and the ClickHouse nodes are
    configured through the environment, and `import chlab_tools` gives shortcuts:
    catalog(), s3(), query(sql), rows(sql), BUCKET, NODES. The current directory is
    mounted at /work (the working directory), so the script reads and writes local files.
    Arguments after the script are passed to it. Exits with the script's exit code.
    """
    import subprocess
    import sys

    from .core.image import ensure_tools_image
    from .core.tools import tools_command

    try:
        spec = Registry().load(name)
        if script == "-":
            command, mounts, stdin = ["python", "-", *ctx.args], [], True
        else:
            path = Path(script).expanduser().resolve()
            if not path.is_file():
                raise SpecError(f"{script} is not a file")
            target = f"/chlab/run/{path.name}"
            command, mounts, stdin = ["python", target, *ctx.args], [(str(path), target)], False
        image = ensure_tools_image()
        docker_command = tools_command(
            spec, command, image=image, workdir=Path.cwd(), mounts=mounts, interactive=stdin
        )
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=subprocess.call(docker_command, stdin=sys.stdin if stdin else None))


@app.command("tools")
def tools_shell_command(
    name: NameArgument,
    command: Annotated[
        list[str] | None,
        typer.Argument(help="Command to run, after `--`. Defaults to bash."),
    ] = None,
) -> None:
    """Run any command in the tools container: jq, mc, curl, python, aws-style boto3 scripts.

    Same environment and /work mount as `chlab py`. Exits with the command's exit code.
    """
    import subprocess
    import sys

    from .core.image import ensure_tools_image
    from .core.tools import tools_command

    try:
        spec = Registry().load(name)
        image = ensure_tools_image()
        docker_command = tools_command(
            spec,
            command or ["bash"],
            image=image,
            workdir=Path.cwd(),
            interactive=True,
            tty=sys.stdin.isatty() and sys.stdout.isatty(),
        )
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=subprocess.call(docker_command))


# --- repro -------------------------------------------------------------------------------


@repro_app.command("init")
def repro_init(
    directory: Annotated[Path, typer.Argument(help="Directory to create.")],
    title: Annotated[str | None, typer.Option("--title", help="One-line description.")] = None,
    with_service: Annotated[
        list[str] | None, typer.Option("--with", "-w", help="Service to include. Repeatable.")
    ] = None,
) -> None:
    """Create a reproduction skeleton: repro.yaml and a first step."""
    from .core import repro

    try:
        repro.init(directory, title=title, services=with_service)
    except ChlabError as failure:
        _fail(failure)
    console.print(f"created {directory / repro.REPRO_FILE} and {directory / '01-setup.sql'}")


@repro_app.command("check")
def repro_check(
    directory: Annotated[Path, typer.Argument(help="Reproduction directory.")],
    json: JsonOption = False,
) -> None:
    """Validate a reproduction without running it: repro.yaml, steps, directives."""
    from .core import repro

    try:
        loaded = repro.load(directory)
    except ChlabError as failure:
        _fail(failure)

    steps = [
        {
            "step": step.name,
            "kind": step.kind,
            "where": step.where,
            "expect": step.expect.describe(),
        }
        for step in loaded.steps
    ]
    if json:
        console.print_json(jsonlib.dumps({"title": loaded.title, "steps": steps}))
        return
    console.print(f"[bold]{escape(loaded.title)}[/bold]")
    table = Table(box=None, padding=(0, 2, 0, 0))
    for column in ("STEP", "RUNS ON", "EXPECTS"):
        table.add_column(column)
    for step in steps:
        table.add_row(step["step"], step["where"], step["expect"])
    console.print(table)


@repro_app.command("run")
def repro_run(
    directory: Annotated[Path, typer.Argument(help="Reproduction directory.")],
    clickhouse: Annotated[
        str | None,
        typer.Option("--clickhouse", "-c", help="Build to run on. Defaults to repro.yaml's."),
    ] = None,
    compare: Annotated[
        str | None,
        typer.Option(
            "--compare",
            help="Also run on this build and report the steps where the two behave differently.",
        ),
    ] = None,
    no_compare: Annotated[
        bool, typer.Option("--no-compare", help="Ignore compare: in repro.yaml.")
    ] = False,
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the instances running for inspection.")
    ] = False,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Where to write result.json and report.md. "
            "Default: <directory>/results/<timestamp>.",
        ),
    ] = None,
    json: Annotated[bool, typer.Option("--json", help="Print result.json to stdout.")] = False,
) -> None:
    """Run a reproduction on fresh instances, then remove them.

    Exits 0 when every step met its expectation on every build, 1 when any step did not —
    expectations describe correct behaviour, so 1 usually means the bug reproduced — and 2
    when a cluster could not be launched.
    """
    from .core import repro

    try:
        loaded = repro.load(directory)
        primary = clickhouse or loaded.clickhouse
        if not primary:
            raise SpecError("give the build with -c, or set clickhouse: in repro.yaml")
        sources = [primary]
        second = compare or (None if no_compare else loaded.compare)
        if second:
            sources.append(second)
    except ChlabError as failure:
        _fail(failure)

    events = error_console if json else console
    with _spinner("starting") as report:

        def notify(message: str) -> None:
            report.event(message)
            if message.endswith(("passed", "FAILED")):
                colour = "green" if message.endswith("passed") else "red"
                events.print(f"[{colour}]{escape(message)}[/{colour}]")

        run = repro.Runner(on_event=notify).run(loaded, sources, keep=keep)

    stamp = run.started.replace(":", "").replace("-", "")
    destination = out or (loaded.directory / "results" / stamp)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "result.json").write_text(jsonlib.dumps(run.to_dict(), indent=2) + "\n")
    (destination / "report.md").write_text(repro.markdown(run))

    if json:
        console.print_json(jsonlib.dumps(run.to_dict()))
    else:
        console.print()
        for build in run.builds:
            if build.error:
                verdict = "[red]launch failed[/red]"
            else:
                verdict = "[green]passed[/green]" if build.passed else "[red]failed[/red]"
            kept = f" (kept as {build.instance})" if build.kept else ""
            console.print(f"{escape(build.source)} ({build.version or '?'}): {verdict}{kept}")
            if build.error:
                error_console.print(escape(build.error.strip()))
        if len(run.builds) == 2 and run.launched:
            console.print(
                "builds differ at: " + ", ".join(run.differences)
                if run.differences
                else "both builds behaved identically"
            )
        console.print(f"[dim]{destination / 'report.md'}[/dim]")

    if not run.launched:
        raise typer.Exit(code=2)
    raise typer.Exit(code=0 if run.passed else 1)


# --- s3 ----------------------------------------------------------------------------------

AsUserOption = Annotated[
    str | None,
    typer.Option("--as", help="Act as this S3 user (from --s3-user) instead of the root user."),
]


def _s3_spec(name: str) -> ClusterSpec:
    spec = Registry().load(name)
    if not spec.has_service("s3"):
        raise SpecError(f"{name} has no s3 service; launch it with --with s3")
    return spec


def _run_mc(name: str, args: list[str], *, as_user=None, mounts=(), stdin=False) -> NoReturn:
    import subprocess

    from .core.services import s3

    try:
        command = s3.mc_command(
            _s3_spec(name), args, as_user=as_user, mounts=mounts, interactive=stdin
        )
    except ChlabError as failure:
        _fail(failure)
    raise typer.Exit(code=subprocess.call(command))


@s3_app.command("ls")
def s3_ls(
    name: NameArgument,
    path: Annotated[str, typer.Argument(help="BUCKET[/PREFIX]. Omit for all buckets.")] = "",
    recursive: Annotated[bool, typer.Option("--recursive", "-r")] = False,
    json: Annotated[bool, typer.Option("--json", help="mc's JSON lines output.")] = False,
    as_user: AsUserOption = None,
) -> None:
    """List buckets or objects."""
    from .core.services.s3 import object_path

    args = ["ls", *(["--recursive"] if recursive else []), *(["--json"] if json else [])]
    _run_mc(name, [*args, object_path(path)], as_user=as_user)


@s3_app.command("put")
def s3_put(
    name: NameArgument,
    paths: Annotated[
        list[str], typer.Argument(help="Local files or directories, then BUCKET/KEY last.")
    ],
    as_user: AsUserOption = None,
) -> None:
    """Upload files or directories: `chlab s3 put lab ./a.parquet ./dir warehouse/data/`.

    As with cp, a destination ending in / is a prefix to copy into.
    """
    from .core.services.s3 import object_path

    if len(paths) < 2:
        _fail(SpecError("give at least one local path and a BUCKET/KEY destination"))
    *sources, destination = paths
    mounts, inside = [], []
    for index, source in enumerate(sources):
        local = Path(source).expanduser()
        if not local.exists():
            _fail(SpecError(f"{source} does not exist"))
        target = f"/chlab-in/{index}/{local.resolve().name}"
        mounts.append((str(local.resolve()), target, True))
        inside.append(target)
    _run_mc(
        name,
        ["cp", "--recursive", *inside, object_path(destination)],
        as_user=as_user,
        mounts=mounts,
    )


@s3_app.command("get")
def s3_get(
    name: NameArgument,
    path: Annotated[str, typer.Argument(help="BUCKET/KEY or BUCKET/PREFIX/.")],
    destination: Annotated[str, typer.Argument(help="Local file or existing directory.")] = ".",
    as_user: AsUserOption = None,
) -> None:
    """Download an object, or a prefix recursively."""
    from .core.services.s3 import object_path

    local = Path(destination).expanduser().resolve()
    if local.is_dir():
        mount, target = local, "/chlab-out/"
    else:
        if not local.parent.is_dir():
            _fail(SpecError(f"{local.parent} does not exist"))
        mount, target = local.parent, f"/chlab-out/{local.name}"
    _run_mc(
        name,
        ["cp", "--recursive", object_path(path), target],
        as_user=as_user,
        mounts=[(str(mount), "/chlab-out", False)],
    )


@s3_app.command("cat")
def s3_cat(
    name: NameArgument,
    path: Annotated[str, typer.Argument(help="BUCKET/KEY.")],
    as_user: AsUserOption = None,
) -> None:
    """Print an object, e.g. an Iceberg metadata.json."""
    from .core.services.s3 import object_path

    _run_mc(name, ["cat", object_path(path)], as_user=as_user)


@s3_app.command("rm")
def s3_rm(
    name: NameArgument,
    path: Annotated[str, typer.Argument(help="BUCKET/KEY, or a prefix with --recursive.")],
    recursive: Annotated[bool, typer.Option("--recursive", "-r")] = False,
    as_user: AsUserOption = None,
) -> None:
    """Delete an object, or everything under a prefix."""
    from .core.services.s3 import object_path

    args = ["rm", *(["--recursive", "--force"] if recursive else []), object_path(path)]
    _run_mc(name, args, as_user=as_user)


@s3_app.command("mb")
def s3_mb(
    name: NameArgument,
    bucket: Annotated[str, typer.Argument(help="Bucket to create.")],
    as_user: AsUserOption = None,
) -> None:
    """Create a bucket after launch (or use --bucket at `chlab up`)."""
    from .core.services.s3 import object_path

    _run_mc(name, ["mb", "--ignore-existing", object_path(bucket)], as_user=as_user)


@s3_app.command("mc")
def s3_mc(
    name: NameArgument,
    args: Annotated[list[str], typer.Argument(help="mc arguments, after `--`. The alias is `s3`.")],
    as_user: AsUserOption = None,
) -> None:
    """Run any mc command against the instance: `chlab s3 mc lab -- stat s3/warehouse/x`.

    Stdin is passed through, so `... -- pipe s3/warehouse/key < file` works.
    """
    _run_mc(name, list(args), as_user=as_user, stdin=True)


# --- profiles ----------------------------------------------------------------------------


@profiles_app.command("ls")
def profiles_ls(json: JsonOption = False) -> None:
    """List saved profiles."""
    saved = profiles_module.Profiles.default().all()
    if json:
        console.print_json(jsonlib.dumps(saved))
        return
    if not saved:
        console.print(
            "[dim]no profiles; save one with `chlab profiles save <name> <instance>`[/dim]"
        )
        return

    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("PROFILE", style="bold")
    table.add_column("TOPOLOGY")
    for profile_name, profile in sorted(saved.items()):
        table.add_row(profile_name, profiles_module.summarise(profile))
    console.print(table)


@profiles_app.command("save")
def profiles_save(
    profile_name: Annotated[str, typer.Argument(help="Name for the profile.")],
    instance: Annotated[str, typer.Argument(help="Instance to copy the topology from.")],
) -> None:
    """Save a running instance's topology as a reusable profile."""
    try:
        spec = Registry().load(instance)
        profiles_module.Profiles.default().save(profile_name, spec)
    except ChlabError as failure:
        _fail(failure)
    console.print(f"saved profile [bold]{profile_name}[/bold] from {instance}")


@profiles_app.command("rm")
def profiles_rm(profile_name: Annotated[str, typer.Argument(help="Profile to delete.")]) -> None:
    """Delete a profile."""
    try:
        profiles_module.Profiles.default().delete(profile_name)
    except ChlabError as failure:
        _fail(failure)
    console.print(f"deleted profile {profile_name}")


# --- web ---------------------------------------------------------------------------------


@app.command("ui")
def ui_command(
    port: Annotated[int, typer.Option("--port", help="Port to serve on.")] = 7777,
    host: Annotated[str, typer.Option("--host", help="Address to bind.")] = "127.0.0.1",
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open a browser window.")
    ] = True,
) -> None:
    """Serve the web interface."""
    import uvicorn

    from .web.app import create_app

    url = f"http://{host}:{port}"
    if host not in {"127.0.0.1", "localhost", "::1"}:
        error_console.print(
            f"[bold yellow]warning:[/bold yellow] binding to {host} exposes an unauthenticated "
            "API that runs SQL and deletes clusters to everyone who can reach this machine."
        )
    console.print(f"chlab ui on [bold]{url}[/bold]")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(create_app(), host=host, port=port, log_level="warning")


# --- output helpers ----------------------------------------------------------------------


class _Report:
    """Adapts resolve()'s callbacks onto a rich progress bar."""

    def __init__(self, progress_bar: Progress, task):
        self._progress = progress_bar
        self._task = task

    def event(self, message: str) -> None:
        self._progress.update(self._task, description=message, completed=0, total=None)

    def progress(self, done: int, total: int | None) -> None:
        self._progress.update(self._task, completed=done, total=total)

    def stop(self) -> None:
        self._progress.stop()


class _ProgressContext:
    def __init__(self, disabled: bool = False, description: str = "working"):
        self._description = description
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            console=console,
            transient=True,
            disable=disabled,
        )

    def __enter__(self) -> _Report:
        self._progress.start()
        task = self._progress.add_task(self._description, total=None)
        self._report = _Report(self._progress, task)
        return self._report

    def __exit__(self, *exc_info) -> None:
        self._progress.stop()


def _download_progress(disabled: bool = False) -> _ProgressContext:
    return _ProgressContext(disabled)


def _spinner(description: str) -> _ProgressContext:
    return _ProgressContext(description=description)


def _print_instance(status: InstanceStatus) -> None:
    spec = status.spec
    header = _kv_table()
    header.add_row("instance", f"[bold]{spec.name}[/bold]")
    header.add_row("state", _state_markup(status.state))
    header.add_row("version", spec.version or "unknown")
    header.add_row("source", spec.source)
    header.add_row("topology", _topology_summary(status))
    header.add_row("cluster", spec.cluster_name)
    if spec.users:
        header.add_row("users", ", ".join(f"{user.name} ({user.role})" for user in spec.users))
    if spec.databases:
        header.add_row("databases", ", ".join(database.name for database in spec.databases))
    if spec.seed:
        header.add_row("demo data", "demo.events (distributed), demo.events_local")
    console.print(header)
    console.print()

    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("NODE", style="bold", overflow="fold")
    table.add_column("SHARD", overflow="fold")
    table.add_column("REPLICA", overflow="fold")
    table.add_column("HTTP", overflow="fold")
    table.add_column("NATIVE", overflow="fold")
    table.add_column("PLAY", overflow="fold")
    for node in status.topology.nodes:
        table.add_row(
            node.name,
            str(node.shard),
            str(node.replica),
            node.http_url,
            f"127.0.0.1:{node.tcp_port}",
            node.play_url,
        )
    for keeper in status.topology.keepers:
        table.add_row(keeper.name, "-", "-", f"127.0.0.1:{keeper.client_port}", "-", "-")
    for service in status.topology.services:
        table.add_row(service.name, "-", "-", f"http://127.0.0.1:{service.port_base}", "-", "-")
    console.print(table)


def _print_result(result: QueryResult) -> None:
    if result.is_empty:
        console.print("[dim]ok[/dim]")
        return

    table = Table(box=None, padding=(0, 2, 0, 0))
    for column in result.columns:
        table.add_column(column)
    for row in result.rows:
        table.add_row(*[_cell(value) for value in row])
    console.print(table)
    console.print(
        f"[dim]{len(result.rows)} row(s), {result.rows_read} read in {result.elapsed:.3f}s[/dim]"
    )


def _print_events(events: dict[str, int]) -> None:
    if not events:
        console.print("[dim]no matching ProfileEvents[/dim]")
        return
    table = Table(box=None, padding=(0, 2, 0, 0))
    table.add_column("EVENT", style="bold")
    table.add_column("VALUE", justify="right")
    for event, value in events.items():
        table.add_row(event, str(value))
    console.print(table)


def _agreement_line(results: list) -> str:
    successes = [result for result in results if isinstance(result, QueryResult)]
    if len(successes) < 2:
        return ""
    fingerprints = {_fingerprint(result) for result in successes}
    if len(fingerprints) == 1:
        return "[green]all nodes agree[/green]"
    return f"[yellow]nodes disagree: {len(fingerprints)} distinct results[/yellow]"


def _fingerprint(result: QueryResult) -> str:
    payload = jsonlib.dumps([result.columns, result.rows], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _topology_summary(status: InstanceStatus) -> str:
    spec = status.spec
    summary = f"{spec.shards}x{spec.replicas} ({spec.node_count} node{'s' if spec.node_count > 1 else ''})"
    if status.topology.keepers:
        summary += f" + {len(status.topology.keepers)} keeper"
    for service in status.topology.services:
        summary += f" + {service.name}"
    return summary


def _state_markup(state: str) -> str:
    colours = {"running": "green", "stopped": "dim", "partial": "yellow", "created": "dim"}
    return f"[{colours.get(state, 'white')}]{state}[/{colours.get(state, 'white')}]"


def _cell(value) -> str:
    if value is None:
        return "[dim]NULL[/dim]"
    if isinstance(value, (dict, list)):
        return jsonlib.dumps(value)
    return str(value)


def _entry_size(cache: Cache, entry: dict) -> int:
    key = None
    for meta_file in (cache.root / "bin").glob("*/meta.json"):
        if meta_file.parent.name and entry.get("source") and meta_file.is_file():
            try:
                if jsonlib.loads(meta_file.read_text()).get("source") == entry["source"]:
                    key = meta_file.parent.name
                    break
            except (OSError, jsonlib.JSONDecodeError):
                continue
    if key is None:
        return 0
    total = 0
    for directory in (cache.bin_dir(key), cache.artifact_dir(key)):
        for path in directory.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    return total


def _human_size(size: int) -> str:
    if not size:
        return "-"
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}G"


def _kv_table() -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bold")
    return table


def _users_with_grants(users: list[str], grants: list[str]) -> tuple:
    """Attach each ``--grant user:...`` to the matching ``--user``."""
    parsed = {spec.name: spec for spec in (parse_user(text) for text in users)}
    for text in grants:
        name, grant = parse_grant(text)
        if name not in parsed:
            raise SpecError(f"--grant refers to user {name!r}, which was not created with --user")
        parsed[name] = replace(parsed[name], grants=(*parsed[name].grants, grant))
    return tuple(parsed.values())


def _lifecycle(action, message: str) -> None:
    runtime = Runtime()
    with _spinner("working"):
        try:
            action(runtime)
        except ChlabError as failure:
            _fail(failure)
    console.print(message)


def _resolved_as_dict(resolved: Resolved) -> dict:
    return {
        "source": resolved.source.raw,
        "kind": resolved.source.kind.value,
        "artifact": resolved.source.artifact.value,
        "version": resolved.version,
        "docker_image": resolved.docker_image,
        "binary_path": str(resolved.binary_path) if resolved.binary_path else None,
        "cached": resolved.cached,
    }


def _status_as_dict(status: InstanceStatus) -> dict:
    return {
        "name": status.spec.name,
        "state": status.state,
        "version": status.spec.version,
        "source": status.spec.source,
        "shards": status.spec.shards,
        "replicas": status.spec.replicas,
        "cluster": status.spec.cluster_name,
        "nodes": [
            {
                "name": node.name,
                "shard": node.shard,
                "replica": node.replica,
                "http": node.http_url,
                "native": node.tcp_port,
                "play": node.play_url,
            }
            for node in status.topology.nodes
        ],
        "keepers": [
            {"name": keeper.name, "port": keeper.client_port} for keeper in status.topology.keepers
        ],
        "services": [
            {"name": service.name, "state": service.state} for service in status.services or ()
        ],
    }


def _result_as_dict(result) -> dict:
    if isinstance(result, QueryError):
        payload = {"node": result.node, "error": str(result), "code": result.code}
    else:
        payload = {
            "node": result.node,
            "columns": result.columns,
            "types": result.types,
            "rows": result.rows,
            "rows_read": result.rows_read,
            "elapsed": result.elapsed,
        }
    if result.query_id:
        payload["query_id"] = result.query_id
    if result.events is not None:
        payload["events"] = result.events
    return payload


def _fail(failure: Exception) -> NoReturn:
    error_console.print(f"[bold red]error:[/bold red] {escape(str(failure))}")
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
