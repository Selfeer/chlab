"""chlab command line."""

from __future__ import annotations

import hashlib
import json as jsonlib
import os
import shutil
from dataclasses import replace
from typing import Annotated, NoReturn

import typer
from rich.console import Console
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
    ClusterSpec,
    SpecError,
    parse_database,
    parse_grant,
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
profiles_app = typer.Typer(help="Saved cluster topologies.", no_args_is_help=True)
app.add_typer(cache_app, name="cache")
app.add_typer(profiles_app, name="profiles")

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
        typer.Option("--setting", help="Server setting as key=value. Repeatable."),
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
    sql: Annotated[str, typer.Argument(help="SQL to run.")],
    node: Annotated[str | None, typer.Option("--node", help="Run on this node.")] = None,
    all_nodes: Annotated[
        bool, typer.Option("--all", "-a", help="Run on every node and compare the results.")
    ] = False,
    user: Annotated[str, typer.Option("--user", "-u", help="ClickHouse user.")] = "default",
    password: Annotated[str, typer.Option("--password", help="ClickHouse password.")] = "",
    database: Annotated[str | None, typer.Option("--database", "-d")] = None,
    json: JsonOption = False,
) -> None:
    """Run SQL against an instance."""
    runtime = Runtime()
    try:
        if all_nodes:
            results = runtime.query_all(name, sql, user=user, password=password, database=database)
        else:
            results = [
                runtime.query(name, sql, node=node, user=user, password=password, database=database)
            ]
    except ChlabError as failure:
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
        return {"node": result.node, "error": str(result)}
    return {
        "node": result.node,
        "columns": result.columns,
        "types": result.types,
        "rows": result.rows,
        "rows_read": result.rows_read,
        "elapsed": result.elapsed,
    }


def _fail(failure: Exception) -> NoReturn:
    error_console.print(f"[bold red]error:[/bold red] {failure}")
    raise typer.Exit(code=1)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
