"""chlab command line."""

from __future__ import annotations

import hashlib
import json as jsonlib
import os
import shutil
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.progress import BarColumn, DownloadColumn, Progress, SpinnerColumn, TextColumn, TransferSpeedColumn
from rich.table import Table

from .core import profiles as profiles_module
from .core.cache import Cache
from .core.download import DownloadError
from .core.image import ImageError
from .core.ports import NoFreePorts
from .core.query import QueryError, QueryResult
from .core.registry import InstanceExists, InstanceNotFound, Registry
from .core.resolve import DEFAULT_PROGRAM, Resolved, ResolveError, resolve
from .core.runtime import InitError, InstanceStatus, Runtime, RuntimeError_
from .core.spec import (
    ClusterSpec,
    SpecError,
    parse_database,
    parse_grant,
    parse_user,
    suggest_name,
)
from .core.sources import UnsupportedSource
from .core.unpack import UnpackError

app = typer.Typer(
    help="Run any ClickHouse build, immediately.",
    no_args_is_help=True,
    add_completion=False,
)
cache_app = typer.Typer(help="Inspect and clear the artifact cache.", no_args_is_help=True)
profiles_app = typer.Typer(help="Saved cluster topologies.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")
app.add_typer(profiles_app, name="profiles")

console = Console()
error_console = Console(stderr=True)

CHLAB_ERRORS = (
    ResolveError,
    UnsupportedSource,
    DownloadError,
    UnpackError,
    SpecError,
    RuntimeError_,
    InitError,
    ImageError,
    NoFreePorts,
    QueryError,
    InstanceExists,
    InstanceNotFound,
    profiles_module.ProfileNotFound,
)

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
        Optional[str], typer.Option("--clickhouse-version", help="Skip detection and use this version.")
    ] = None,
    force: Annotated[bool, typer.Option("--force", "-f", help="Ignore the cache.")] = False,
    program: Annotated[str, typer.Option("--program", hidden=True)] = DEFAULT_PROGRAM,
    json: JsonOption = False,
) -> None:
    """Resolve a ClickHouse source to a runnable image or binary."""
    with _download_progress(disabled=json) as report:
        try:
            resolved = resolve(
                source,
                program=program,
                version=version,
                force=force,
                on_event=report.event,
                on_progress=report.progress,
            )
        except CHLAB_ERRORS as failure:
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
    table.add_column("SOURCE", overflow="fold")
    for entry in sorted(entries, key=lambda item: item.get("version") or ""):
        table.add_row(entry.get("version") or "unknown", entry.get("kind", ""), entry.get("source", ""))
    console.print(table)


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
        Optional[str], typer.Option("--name", "-n", help="Instance name. Derived from the version if omitted.")
    ] = None,
    version: Annotated[
        Optional[str], typer.Option("--clickhouse-version", help="Skip detection and use this version.")
    ] = None,
    shards: Annotated[Optional[int], typer.Option("--shards", "-s", help="Number of shards.")] = None,
    replicas: Annotated[
        Optional[int], typer.Option("--replicas", "-r", help="Replicas per shard.")
    ] = None,
    keeper: Annotated[
        Optional[int],
        typer.Option("--keeper", "-k", help="Keeper nodes. Defaults to 1 when replicating, else 0."),
    ] = None,
    user: Annotated[
        Optional[list[str]],
        typer.Option(
            "--user",
            "-u",
            help="Create a user: name[:password][:none|readonly|readwrite|admin]. "
            "Defaults to readwrite. Repeatable.",
        ),
    ] = None,
    grant: Annotated[
        Optional[list[str]],
        typer.Option("--grant", help="Extra grant: user:PRIVILEGES ON target. Repeatable."),
    ] = None,
    database: Annotated[
        Optional[list[str]],
        typer.Option("--database", "-d", help="Create a database: name[:Atomic|Replicated]. Repeatable."),
    ] = None,
    setting: Annotated[
        Optional[list[str]],
        typer.Option("--setting", help="Server setting as key=value. Repeatable."),
    ] = None,
    seed: Annotated[
        bool, typer.Option("--seed", help="Create the demo database with generated rows.")
    ] = False,
    cluster_name: Annotated[
        Optional[str], typer.Option("--cluster-name", help="Name of the generated cluster.")
    ] = None,
    profile: Annotated[
        Optional[str], typer.Option("--profile", "-p", help="Start from a saved profile.")
    ] = None,
    force_resolve: Annotated[
        bool, typer.Option("--force-resolve", help="Re-download the source, ignoring the cache.")
    ] = False,
) -> None:
    """Launch a ClickHouse cluster."""
    runtime = Runtime()
    registry = runtime.registry

    overrides: dict = {
        "shards": shards,
        "replicas": replicas,
        "keeper_nodes": keeper,
        "cluster_name": cluster_name,
        "seed": seed or None,
    }
    if user:
        overrides["users"] = _users_with_grants(user, grant or [])
    elif grant:
        _fail(SpecError("--grant needs a matching --user"))
    if database:
        overrides["databases"] = tuple(parse_database(text) for text in database)
    if setting:
        overrides["settings"] = dict(_parse_setting(text) for text in setting)

    try:
        base = profiles_module.Profiles.default().get(profile) if profile else {}
        fields = profiles_module.apply(base, overrides)
        instance_name = name or suggest_name(
            clickhouse, taken={spec.name for spec in registry.list()}
        )
        spec = ClusterSpec(name=instance_name, source=clickhouse, version=version, **fields)
    except CHLAB_ERRORS as failure:
        _fail(failure)

    with _download_progress() as report:
        try:
            status = runtime.up(spec, force_resolve=force_resolve, on_event=report.event)
        except CHLAB_ERRORS as failure:
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
    except CHLAB_ERRORS as failure:
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
    with _spinner("starting"):
        try:
            status = runtime.start(name)
        except CHLAB_ERRORS as failure:
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
        Optional[str], typer.Argument(help="Node or keeper name. Defaults to all services.")
    ] = None,
    follow: Annotated[bool, typer.Option("--follow", "-f", help="Stream new output.")] = False,
    tail: Annotated[int, typer.Option("--tail", help="Lines of history to show.")] = 200,
) -> None:
    """Show server logs."""
    runtime = Runtime()
    try:
        code = runtime.logs(name, service, follow=follow, tail=tail)
    except CHLAB_ERRORS as failure:
        _fail(failure)
    raise typer.Exit(code=code)


# --- querying ----------------------------------------------------------------------------


@app.command("query")
def query_command(
    name: NameArgument,
    sql: Annotated[str, typer.Argument(help="SQL to run.")],
    node: Annotated[Optional[str], typer.Option("--node", help="Run on this node.")] = None,
    all_nodes: Annotated[
        bool, typer.Option("--all", "-a", help="Run on every node and compare the results.")
    ] = False,
    user: Annotated[str, typer.Option("--user", "-u", help="ClickHouse user.")] = "default",
    password: Annotated[str, typer.Option("--password", help="ClickHouse password.")] = "",
    database: Annotated[Optional[str], typer.Option("--database", "-d")] = None,
    json: JsonOption = False,
) -> None:
    """Run SQL against an instance."""
    runtime = Runtime()
    try:
        if all_nodes:
            results = runtime.query_all(name, sql, user=user, password=password, database=database)
        else:
            results = [runtime.query(name, sql, node=node, user=user, password=password, database=database)]
    except CHLAB_ERRORS as failure:
        _fail(failure)

    if json:
        console.print_json(jsonlib.dumps([_result_as_dict(result) for result in results]))
        return

    for result in results:
        if isinstance(result, QueryError):
            error_console.print(f"[bold red]{result.node}:[/bold red] {result}")
            continue
        if all_nodes:
            console.print(f"[bold]{result.node}[/bold]")
        _print_result(result)

    if all_nodes:
        console.print(_agreement_line(results))


@app.command("client")
def client_command(
    name: NameArgument,
    node: Annotated[Optional[str], typer.Argument(help="Node to connect to. Defaults to the first.")] = None,
) -> None:
    """Open an interactive clickhouse-client session on a node."""
    runtime = Runtime()
    try:
        command = runtime.client_command(name, node)
    except (KeyError, *CHLAB_ERRORS) as failure:
        _fail(failure)
    # Hand the terminal over so the REPL behaves exactly as it would run directly.
    os.execvp(command[0], command)


# --- profiles ----------------------------------------------------------------------------


@profiles_app.command("ls")
def profiles_ls(json: JsonOption = False) -> None:
    """List saved profiles."""
    saved = profiles_module.Profiles.default().all()
    if json:
        console.print_json(jsonlib.dumps(saved))
        return
    if not saved:
        console.print("[dim]no profiles; save one with `chlab profiles save <name> <instance>`[/dim]")
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
    except CHLAB_ERRORS as failure:
        _fail(failure)
    console.print(f"saved profile [bold]{profile_name}[/bold] from {instance}")


@profiles_app.command("rm")
def profiles_rm(profile_name: Annotated[str, typer.Argument(help="Profile to delete.")]) -> None:
    """Delete a profile."""
    try:
        profiles_module.Profiles.default().delete(profile_name)
    except CHLAB_ERRORS as failure:
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
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        _fail(RuntimeError("the web interface needs fastapi and uvicorn; pip install 'chlab[web]'"))

    url = f"http://{host}:{port}"
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
    def __init__(self, disabled: bool = False):
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
        task = self._progress.add_task("working", total=None)
        self._report = _Report(self._progress, task)
        return self._report

    def __exit__(self, *exc_info) -> None:
        self._progress.stop()


def _download_progress(disabled: bool = False) -> _ProgressContext:
    return _ProgressContext(disabled)


def _spinner(description: str) -> _ProgressContext:
    return _ProgressContext()


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
    console.print(table)


def _print_result(result: QueryResult) -> None:
    if result.is_empty:
        console.print("[dim]ok[/dim]")
        return

    table = Table(box=None, padding=(0, 2, 0, 0))
    for column in result.columns:
        table.add_column(column, style="bold" if False else None)
    for row in result.rows:
        table.add_row(*[_cell(value) for value in row])
    console.print(table)
    console.print(
        f"[dim]{len(result.rows)} row(s), {result.rows_read} read in {result.elapsed:.3f}s[/dim]"
    )


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


def _kv_table() -> Table:
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="bold")
    return table


def _users_with_grants(users: list[str], grants: list[str]) -> tuple:
    """Attach each ``--grant user:...`` to the matching ``--user``."""
    from dataclasses import replace

    parsed = {spec.name: spec for spec in (parse_user(text) for text in users)}
    for text in grants:
        name, grant = parse_grant(text)
        if name not in parsed:
            raise SpecError(f"--grant refers to user {name!r}, which was not created with --user")
        parsed[name] = replace(parsed[name], grants=(*parsed[name].grants, grant))
    return tuple(parsed.values())


def _parse_setting(text: str) -> tuple[str, str]:
    key, separator, value = text.partition("=")
    if not separator:
        raise SpecError(f"setting {text!r} must be written key=value")
    return key.strip(), value.strip()


def _lifecycle(action, message: str) -> None:
    runtime = Runtime()
    with _spinner("working"):
        try:
            action(runtime)
        except CHLAB_ERRORS as failure:
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
            {"name": service.name, "state": service.state} for service in status.services
        ],
    }


def _result_as_dict(result) -> dict:
    if isinstance(result, QueryError):
        return {"node": result.node, "error": str(result)}
    return {
        "node": result.node,
        "columns": result.columns,
        "types": result.types,
        "rows": result.rows,
        "rows_read": result.rows_read,
        "elapsed": result.elapsed,
    }


def _fail(failure: Exception) -> None:
    error_console.print(f"[bold red]error:[/bold red] {failure}")
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
