"""Reproductions as directories: a description of the cluster, and steps to replay.

    repro/2792-binpack/
      repro.yaml               the cluster: topology, services, users, settings
      01-setup.sql             run with clickhouse-client --multiquery on a node
      02-optimize.sql
      02-optimize.reference    expected output of the step, as in ClickHouse's own tests
      03-check.sh              run with `sh -e` in the tools container, or on a node with
                               `# chlab: node`
      04-restart.chlab         chlab commands: `restart`, `s3 put ...`, `cp ...`
      05-verify.py             run in the tools container, with pyiceberg and chlab_tools
      06-v3.spark              Spark SQL, for format v3 and deletion vectors (`with: [spark]`)

Steps run in file-name order on a fresh instance. Expectations state the *correct*
behaviour, as a regression test would; a reproduced bug shows up as a failed step. That way a
reproduction becomes the regression test for its fix without being rewritten.

Expectations come from a ``.reference`` file next to the step (exact output) and from
directives in the step's comments:

    -- chlab: expect error 60        the step fails, with this ClickHouse error code
    -- chlab: expect ok              the default
    -- chlab: expect contains TEXT   output contains TEXT (repeatable)
    -- chlab: expect empty           no output
    -- chlab: node clickhouse2       which node runs it (.sql), or run a .sh on that node
    -- chlab: user dev:pw            run SQL as this user
    -- chlab: setting key=value      query setting for SQL (repeatable)
    -- chlab: timeout 300            seconds, default 600

``#`` starts directives in .sh, .py and .chlab files.
"""

from __future__ import annotations

import datetime
import difflib
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from . import profiles as profiles_module
from .errors import ChlabError
from .events import EventCallback, silent
from .query import error_code
from .runtime import Runtime
from .spec import (
    NAME_PATTERN,
    ClusterSpec,
    SpecError,
    parse_database,
    parse_grant,
    parse_s3_user,
    parse_setting,
    parse_user,
)

REPRO_FILE = "repro.yaml"
STEP_KINDS = {".sql": "sql", ".sh": "sh", ".py": "py", ".chlab": "chlab", ".spark": "spark"}
DEFAULT_TIMEOUT = 600
#: Output kept per step in the JSON result and the report. Enough to read; not a log dump.
MAX_OUTPUT = 20_000

_DIRECTIVE = re.compile(r"^\s*(?:--|#)\s*chlab:\s*([a-z_]+)\s*(.*?)\s*$")

#: Keys repro.yaml may use, and the ClusterSpec field or parser each maps to.
SPEC_KEYS = {
    "title",
    "clickhouse",
    "compare",
    "shards",
    "replicas",
    "keeper",
    "cluster_name",
    "users",
    "grants",
    "databases",
    "settings",
    "query_settings",
    "with",
    "buckets",
    "s3_users",
    "seed",
}

#: chlab commands a .chlab step may run. Lifecycle commands that would end the instance
#: (`rm`, `down`) are deliberately absent: the runner owns the instance's life.
CHLAB_ACTIONS = {
    "restart",
    "stop",
    "start",
    "exec",
    "cp",
    "s3",
    "query",
    "py",
    "tools",
    "logs",
    "proxy",
    "spark",
}


class ReproError(ChlabError):
    pass


# --- loading -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    #: "ok" or "error".
    outcome: str = "ok"
    #: For "error": the ClickHouse error code required, if any.
    code: int | None = None
    contains: tuple[str, ...] = ()
    empty: bool = False
    #: Exact expected output, from the .reference file.
    reference: str | None = None

    def describe(self) -> str:
        parts = [f"error {self.code}" if self.code is not None else self.outcome]
        parts += [f"contains {text!r}" for text in self.contains]
        if self.empty:
            parts.append("no output")
        if self.reference is not None:
            parts.append("matches .reference")
        return ", ".join(parts)


@dataclass(frozen=True)
class Step:
    path: Path
    kind: str
    expect: Expectation = Expectation()
    node: str | None = None
    user: str = "default"
    password: str = ""
    settings: dict[str, str] = field(default_factory=dict)
    timeout: int = DEFAULT_TIMEOUT

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def where(self) -> str:
        if self.kind == "sql":
            return self.node or "first node"
        if self.kind == "sh":
            return self.node or "tools container"
        if self.kind == "py":
            return "tools container"
        if self.kind == "spark":
            return "spark"
        return "chlab"


@dataclass(frozen=True)
class Repro:
    directory: Path
    title: str
    #: ClusterSpec fields, without name and source.
    fields: dict[str, Any]
    steps: tuple[Step, ...]
    #: Defaults for the builds, from repro.yaml; the command line overrides them.
    clickhouse: str | None = None
    compare: str | None = None


def load(directory: Path) -> Repro:
    directory = directory.expanduser()
    config_path = directory / REPRO_FILE
    if not config_path.is_file():
        raise ReproError(f"{directory} has no {REPRO_FILE}; create one with `chlab repro init`")
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError as error:
        raise ReproError(f"{config_path} is not valid YAML: {error}") from error
    if not isinstance(config, dict):
        raise ReproError(f"{config_path} must be a mapping of settings")

    unknown = sorted(set(config) - SPEC_KEYS)
    if unknown:
        raise ReproError(
            f"{config_path}: unknown key(s) {', '.join(unknown)}; "
            f"expected some of {', '.join(sorted(SPEC_KEYS))}"
        )

    steps = tuple(
        parse_step(path)
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix in STEP_KINDS
    )
    if not steps:
        raise ReproError(f"{directory} has no steps (.sql, .sh, .py, .chlab or .spark files)")

    fields = spec_fields(config)
    # Validate now, with placeholder identity, so a typo fails before anything launches.
    ClusterSpec(name="check", source="/check", **fields)
    return Repro(
        directory=directory,
        title=str(config.get("title") or directory.name),
        fields=fields,
        steps=steps,
        clickhouse=config.get("clickhouse"),
        compare=config.get("compare"),
    )


def spec_fields(config: dict[str, Any]) -> dict[str, Any]:
    """Translate repro.yaml into ClusterSpec fields, with the same parsers as `chlab up`."""

    def strings(key: str) -> list[str]:
        value = config.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise ReproError(f"{key} must be a list")
        return [str(item) for item in value]

    def mapping(key: str) -> dict[str, str]:
        value = config.get(key) or {}
        if not isinstance(value, dict):
            raise ReproError(f"{key} must be a mapping of name: value")
        return {str(k): _setting_text(v) for k, v in value.items()}

    overrides: dict[str, Any] = {
        "shards": config.get("shards"),
        "replicas": config.get("replicas"),
        "keeper_nodes": config.get("keeper"),
        "cluster_name": config.get("cluster_name"),
        "seed": config.get("seed"),
    }
    users = {user.name: user for user in (parse_user(text) for text in strings("users"))}
    for text in strings("grants"):
        name, grant = parse_grant(text)
        if name not in users:
            raise ReproError(f"grant {text!r} names user {name!r}, which is not in users")
        users[name] = replace(users[name], grants=(*users[name].grants, grant))
    if users:
        overrides["users"] = tuple(users.values())
    if config.get("databases"):
        overrides["databases"] = tuple(parse_database(text) for text in strings("databases"))
    settings = mapping("settings")
    if settings:
        overrides["settings"] = {parse_setting(f"{k}={v}")[0]: v for k, v in settings.items()}
    query_settings = mapping("query_settings")
    if query_settings:
        overrides["profile_settings"] = {
            parse_setting(f"{k}={v}")[0]: v for k, v in query_settings.items()
        }
    if config.get("with"):
        overrides["services"] = tuple(dict.fromkeys(strings("with")))
    if config.get("buckets"):
        overrides["buckets"] = tuple(strings("buckets"))
    if config.get("s3_users"):
        overrides["s3_users"] = tuple(parse_s3_user(text) for text in strings("s3_users"))
    return profiles_module.apply({}, overrides)


def _setting_text(value: Any) -> str:
    # YAML reads `1` as an int and `true` as a bool; ClickHouse wants 1 and 1.
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def parse_step(path: Path) -> Step:
    kind = STEP_KINDS[path.suffix]
    text = path.read_text()
    outcome, code, contains, empty = "ok", None, [], False
    options: dict[str, Any] = {"settings": {}}

    for number, line in enumerate(text.splitlines(), start=1):
        match = _DIRECTIVE.match(line)
        if not match:
            continue
        directive, argument = match.groups()
        where = f"{path.name}:{number}"
        if directive == "expect":
            word, _, rest = argument.partition(" ")
            if word == "ok":
                outcome = "ok"
            elif word == "error":
                outcome = "error"
                if rest.strip():
                    if not rest.strip().isdigit():
                        raise ReproError(f"{where}: `expect error` takes a numeric error code")
                    code = int(rest.strip())
            elif word == "contains":
                if not rest:
                    raise ReproError(f"{where}: `expect contains` needs the text to look for")
                contains.append(rest)
            elif word == "empty":
                empty = True
            else:
                raise ReproError(
                    f"{where}: unknown expectation {word!r}; use ok, error [CODE], "
                    "contains TEXT or empty"
                )
        elif directive == "node":
            options["node"] = argument
        elif directive == "user":
            user, _, password = argument.partition(":")
            options["user"], options["password"] = user, password
        elif directive == "setting":
            try:
                key, value = parse_setting(argument)
            except SpecError as failure:
                raise ReproError(f"{where}: {failure}") from failure
            options["settings"][key] = value
        elif directive == "timeout":
            if not argument.isdigit():
                raise ReproError(f"{where}: timeout is a number of seconds")
            options["timeout"] = int(argument)
        else:
            raise ReproError(
                f"{where}: unknown directive {directive!r}; "
                "expected expect, node, user, setting or timeout"
            )

    if kind in {"py", "chlab", "spark"} and options.get("node"):
        raise ReproError(f"{path.name}: `node` applies to .sql and .sh steps only")
    if kind != "sql" and (options["settings"] or "user" in options):
        raise ReproError(f"{path.name}: `user` and `setting` apply to .sql steps only")

    reference_path = path.with_suffix(".reference")
    reference = reference_path.read_text() if reference_path.is_file() else None
    expect = Expectation(
        outcome=outcome, code=code, contains=tuple(contains), empty=empty, reference=reference
    )
    return Step(path=path, kind=kind, expect=expect, **options)


# --- running -----------------------------------------------------------------------------


@dataclass
class StepResult:
    step: str
    kind: str
    where: str
    expected: str
    exit_code: int
    #: ClickHouse error code, when the failure came from the server.
    error_code: int | None
    output: str
    error_output: str
    duration: float
    passed: bool
    #: Why the expectation failed, one line each.
    failures: list[str] = field(default_factory=list)
    #: Unified diff against .reference, when there is a mismatch.
    diff: str | None = None


@dataclass
class BuildRun:
    source: str
    instance: str
    version: str | None = None
    #: Set when the cluster could not be launched; then there are no step results.
    error: str | None = None
    steps: list[StepResult] = field(default_factory=list)
    kept: bool = False

    @property
    def passed(self) -> bool:
        return self.error is None and all(result.passed for result in self.steps)


@dataclass
class ReproRun:
    title: str
    directory: str
    started: str
    builds: list[BuildRun]
    #: Steps whose outcome or output differed between builds; only with two builds.
    differences: list[str] = field(default_factory=list)

    @property
    def launched(self) -> bool:
        return all(build.error is None for build in self.builds)

    @property
    def passed(self) -> bool:
        return all(build.passed for build in self.builds)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        for build, data in zip(self.builds, payload["builds"], strict=True):
            data["passed"] = build.passed
        return payload


class Runner:
    def __init__(self, runtime: Runtime | None = None, *, on_event: EventCallback | None = None):
        self.runtime = runtime or Runtime()
        self.notify = on_event or silent

    def run(self, repro: Repro, sources: list[str], *, keep: bool = False) -> ReproRun:
        started = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        taken = {spec.name for spec in self.runtime.registry.list()}
        builds = []
        for index, source in enumerate(sources, start=1):
            name = _instance_name(repro.directory.name, index, taken)
            taken.add(name)
            builds.append(self._run_build(repro, source, name, keep=keep))

        run = ReproRun(
            title=repro.title, directory=str(repro.directory), started=started, builds=builds
        )
        if len(builds) == 2 and run.launched:
            run.differences = _differences(builds[0], builds[1])
        return run

    def _run_build(self, repro: Repro, source: str, name: str, *, keep: bool) -> BuildRun:
        build = BuildRun(source=source, instance=name)
        spec = ClusterSpec(name=name, source=source, **repro.fields)
        self.notify(f"{source}: launching {name}")
        try:
            try:
                status = self.runtime.up(spec, on_event=lambda m: self.notify(f"{name}: {m}"))
            except ChlabError as failure:
                build.error = str(failure)
                return build
            build.version = status.spec.version
            for step in repro.steps:
                self.notify(f"{name}: {step.name}")
                result = self._run_step(status.spec, step, repro.directory)
                build.steps.append(result)
                self.notify(f"{name}: {step.name} {'passed' if result.passed else 'FAILED'}")
        finally:
            if keep:
                build.kept = self.runtime.registry.exists(name)
            elif self.runtime.registry.exists(name):
                self.notify(f"removing {name}")
                try:
                    self.runtime.destroy(name)
                except ChlabError as failure:
                    self.notify(f"could not remove {name}: {failure}")
        return build

    def _run_step(self, spec: ClusterSpec, step: Step, directory: Path) -> StepResult:
        started = time.monotonic()
        try:
            command, stdin, cwd = self._command(spec, step, directory)
            completed = subprocess.run(
                command,
                input=stdin,
                cwd=cwd,
                env=self._child_env() if step.kind == "chlab" else None,
                capture_output=True,
                text=True,
                timeout=step.timeout,
            )
            exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
        except subprocess.TimeoutExpired as expired:
            exit_code = 124
            stdout = _text(expired.stdout)
            stderr = _text(expired.stderr) + f"\nstep timed out after {step.timeout}s"
        except ChlabError as failure:
            exit_code, stdout, stderr = 1, "", str(failure)
        duration = time.monotonic() - started

        code = error_code(stderr) if exit_code != 0 and step.kind == "sql" else None
        failures, diff = evaluate(step.expect, exit_code, code, stdout)
        return StepResult(
            step=step.name,
            kind=step.kind,
            where=step.where,
            expected=step.expect.describe(),
            exit_code=exit_code,
            error_code=code,
            output=_clip(stdout),
            error_output=_clip(stderr),
            duration=round(duration, 3),
            passed=not failures,
            failures=failures,
            diff=diff,
        )

    def _child_env(self) -> dict[str, str]:
        """.chlab steps run the CLI as a child; it must see the instances this runner sees."""
        from .cache import ENV_CACHE_DIR
        from .registry import ENV_STATE_DIR

        return {
            **os.environ,
            ENV_STATE_DIR: str(self.runtime.registry.root),
            ENV_CACHE_DIR: str(self.runtime.cache.root),
        }

    def _command(
        self, spec: ClusterSpec, step: Step, directory: Path
    ) -> tuple[list[str], str | None, Path]:
        """The command for a step, what to feed it on stdin, and where to run it."""
        from .image import ensure_tools_image
        from .tools import tools_command

        script = step.path.read_text()
        if step.kind == "sql":
            command = self.runtime.client_script_command(
                spec.name,
                node=step.node,
                user=step.user,
                password=step.password,
                settings=step.settings,
            )
            return command, script, directory
        if step.kind == "sh" and step.node:
            # -e: a reproduction step that hits an error stops there, rather than carrying on
            # and printing something misleading from a state it never reached.
            command = self.runtime.exec_command(
                spec.name, ["sh", "-e", "-s"], service=step.node, interactive=True
            )
            return command, script, directory
        if step.kind == "spark":
            from .services.spark import spark_sql_command

            if not spec.has_service("spark"):
                raise ReproError(f"{step.name} is a Spark step; add spark to `with:`")
            return spark_sql_command(spec), script, directory
        if step.kind in {"sh", "py"}:
            interpreter = ["sh", "-e", "-s"] if step.kind == "sh" else ["python", "-"]
            command = tools_command(
                spec, interpreter, image=ensure_tools_image(), workdir=directory, interactive=True
            )
            return command, script, directory
        return _chlab_script(spec.name, script), None, directory


def _chlab_script(instance: str, script: str) -> list[str]:
    """A .chlab step as one shell command running each line through the chlab CLI.

    Each line is a chlab command without the instance name, which is inserted after the
    command (and after the subcommand, for `s3`). `{instance}` is replaced where a line
    needs the name somewhere else, as in `cp ./x lab:/path` → `cp ./x {instance}:/path`.
    """
    commands = []
    for number, raw in enumerate(script.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        words = shlex.split(line)
        if words[0] not in CHLAB_ACTIONS:
            raise ReproError(
                f"line {number}: `{words[0]}` is not allowed in a .chlab step; "
                f"use one of {', '.join(sorted(CHLAB_ACTIONS))}"
            )
        if "{instance}" in line:
            words = [word.replace("{instance}", instance) for word in words]
        elif words[0] in {"s3", "proxy"} and len(words) > 1:
            words = [words[0], words[1], instance, *words[2:]]
        else:
            words = [words[0], instance, *words[1:]]
        commands.append(shlex.join([sys.executable, "-m", "chlab.cli", *words]))
    if not commands:
        raise ReproError("the .chlab step has no commands")
    # set -e: stop at the first failing command, and fail the step with its exit code.
    return ["sh", "-c", "set -e\n" + "\n".join(commands)]


def evaluate(
    expect: Expectation, exit_code: int, code: int | None, output: str
) -> tuple[list[str], str | None]:
    """Check one step's outcome against its expectation."""
    failures: list[str] = []
    diff = None
    if expect.outcome == "ok" and exit_code != 0:
        detail = f" (ClickHouse error {code})" if code is not None else ""
        failures.append(f"expected success, exited {exit_code}{detail}")
    if expect.outcome == "error":
        if exit_code == 0:
            failures.append("expected an error, but the step succeeded")
        elif expect.code is not None and code != expect.code:
            failures.append(f"expected ClickHouse error {expect.code}, got {code}")
    for text in expect.contains:
        if text not in output:
            failures.append(f"output does not contain {text!r}")
    if expect.empty and output.strip():
        failures.append("expected no output")
    if expect.reference is not None and _normalise(output) != _normalise(expect.reference):
        failures.append("output differs from .reference")
        diff = "".join(
            difflib.unified_diff(
                _normalise(expect.reference).splitlines(keepends=True),
                _normalise(output).splitlines(keepends=True),
                fromfile="reference",
                tofile="actual",
            )
        )
    return failures, diff


def _differences(first: BuildRun, second: BuildRun) -> list[str]:
    differing = []
    for a, b in zip(first.steps, second.steps, strict=False):
        if (a.passed, a.exit_code, a.error_code, _normalise(a.output)) != (
            b.passed,
            b.exit_code,
            b.error_code,
            _normalise(b.output),
        ):
            differing.append(a.step)
    return differing


def _normalise(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip("\n").splitlines()) + "\n"


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    return text[:MAX_OUTPUT] + f"\n… {len(text) - MAX_OUTPUT} more characters\n"


def _text(value) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _instance_name(directory_name: str, index: int, taken: set[str]) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", directory_name.lower()).strip("-")[:20] or "repro"
    for suffix in range(index, index + 100):
        name = f"repro-{slug}-{suffix}"
        if NAME_PATTERN.match(name) and name not in taken:
            return name
    raise ReproError(f"could not find a free instance name for {directory_name}")


# --- reporting ---------------------------------------------------------------------------


def markdown(run: ReproRun) -> str:
    """A report short enough to paste under a PR."""
    lines = [f"# Reproduction: {run.title}", ""]
    for build in run.builds:
        verdict = "launch failed" if build.error else ("passed" if build.passed else "FAILED")
        lines.append(f"- `{build.source}` — {build.version or 'unknown version'}: **{verdict}**")
    if len(run.builds) == 2 and run.launched:
        if run.differences:
            lines.append(
                "- Builds differ at: " + ", ".join(f"`{step}`" for step in run.differences)
            )
        else:
            lines.append("- Both builds behaved identically.")
    lines += ["", f"Run at {run.started} from `{run.directory}`.", ""]

    for build in run.builds:
        lines += [f"## {build.version or build.source}", ""]
        if build.error:
            lines += ["The cluster did not start:", "", "```", build.error.strip(), "```", ""]
            continue
        lines += ["| Step | Where | Expected | Result |", "|---|---|---|---|"]
        for result in build.steps:
            outcome = "pass" if result.passed else "**FAIL**: " + "; ".join(result.failures)
            lines.append(f"| `{result.step}` | {result.where} | {result.expected} | {outcome} |")
        lines.append("")
        for result in build.steps:
            if result.passed:
                continue
            lines += [f"### `{result.step}`", ""]
            if result.diff:
                lines += ["```diff", result.diff.rstrip(), "```", ""]
            elif result.output.strip():
                lines += ["Output:", "", "```", result.output.strip(), "```", ""]
            if result.error_output.strip():
                lines += ["Errors:", "", "```", result.error_output.strip(), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


INIT_YAML = """\
# What this reproduces, in one line.
title: {title}

# Defaults for `chlab repro run`; -c and --compare override them.
# clickhouse: docker://altinity/clickhouse-server:<tag>
# compare: docker://altinity/clickhouse-server:<base tag>

# The cluster, with the same values `chlab up` takes.
shards: 1
replicas: 1
{with_line}
# keeper: 1
# users: ["dev:pw:readonly"]
# grants: ["dev:NAMED COLLECTION ON s3"]
# databases: ["analytics:Replicated"]
# settings: {{max_concurrent_queries: 200}}        # server, config.d
# query_settings: {{allow_insert_into_iceberg: 1}} # every session
# buckets: [warehouse, other]
# s3_users: ["reader:readerpass:readonly:warehouse"]
# seed: true
"""

INIT_STEP = """\
-- Steps run in file-name order. Write expectations for the CORRECT behaviour;
-- a reproduced bug then shows as a failed step. Put expected output in
-- 01-setup.reference, or use directives:
--   -- chlab: expect error 60
--   -- chlab: expect contains some text
--   -- chlab: node clickhouse2
--   -- chlab: setting allow_insert_into_iceberg=1
SELECT version();
"""


def init(directory: Path, *, title: str | None = None, services: list[str] | None = None) -> None:
    if (directory / REPRO_FILE).exists():
        raise ReproError(f"{directory / REPRO_FILE} already exists")
    directory.mkdir(parents=True, exist_ok=True)
    with_line = f"with: [{', '.join(services)}]" if services else "# with: [s3, iceberg-rest]"
    (directory / REPRO_FILE).write_text(
        INIT_YAML.format(title=title or directory.name, with_line=with_line)
    )
    step = directory / "01-setup.sql"
    if not step.exists():
        step.write_text(INIT_STEP)
