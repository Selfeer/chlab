"""Reproduction directories: loading, directives, evaluation, running and reporting.

The runner is driven against a stubbed runtime; no containers are started.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chlab.cli import app
from chlab.core import repro
from chlab.core.repro import Expectation, ReproError, evaluate
from chlab.core.spec import ClusterSpec

EXAMPLE = Path(__file__).resolve().parent.parent / "examples/repro/2792-metadata-log"


def write(directory: Path, files: dict[str, str]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (directory / name).write_text(content)
    return directory


def minimal(tmp_path: Path, **files: str) -> Path:
    return write(tmp_path / "r", {"repro.yaml": "title: t\n", **files})


# --- loading -------------------------------------------------------------------------------


def test_the_shipped_example_is_valid():
    loaded = repro.load(EXAMPLE)
    assert [step.kind for step in loaded.steps] == ["sql", "sql", "sh", "chlab", "sql"]
    assert loaded.steps[2].node == "clickhouse1"
    assert loaded.steps[2].expect.reference.startswith("metadata-log ends")


def test_steps_run_in_name_order_and_other_files_are_data(tmp_path: Path):
    loaded = repro.load(
        minimal(
            tmp_path,
            **{"10-b.sql": "SELECT 1", "02-a.py": "", "data.csv": "x", "02-a.reference": ""},
        )
    )
    assert [step.name for step in loaded.steps] == ["02-a.py", "10-b.sql"]


def test_directives_are_read_from_comments(tmp_path: Path):
    step = repro.parse_step(
        write(
            tmp_path,
            {
                "s.sql": (
                    "-- chlab: node clickhouse2\n"
                    "-- chlab: user dev:p:w\n"
                    "-- chlab: setting max_threads=1\n"
                    "--chlab: expect error 60\n"
                    "-- chlab: timeout 5\n"
                    "SELECT 1\n"
                )
            },
        )
        / "s.sql"
    )
    assert (step.node, step.user, step.password, step.timeout) == ("clickhouse2", "dev", "p:w", 5)
    assert step.settings == {"max_threads": "1"}
    assert (step.expect.outcome, step.expect.code) == ("error", 60)


def test_hash_directives_and_repeated_contains(tmp_path: Path):
    step = repro.parse_step(
        write(tmp_path, {"s.sh": "# chlab: expect contains a b\n# chlab: expect contains c\n"})
        / "s.sh"
    )
    assert step.expect.contains == ("a b", "c")


@pytest.mark.parametrize(
    "name, content, message",
    [
        ("s.sql", "-- chlab: expect maybe\n", "unknown expectation"),
        ("s.sql", "-- chlab: expect error lots\n", "numeric error code"),
        ("s.sql", "-- chlab: frobnicate\n", "unknown directive"),
        ("s.sql", "-- chlab: timeout soon\n", "number of seconds"),
        ("s.py", "# chlab: node clickhouse1\n", "only"),
        ("s.sh", "# chlab: user dev\n", ".sql steps only"),
    ],
)
def test_bad_directives_point_at_the_line(tmp_path: Path, name, content, message):
    with pytest.raises(ReproError, match=message):
        repro.parse_step(write(tmp_path, {name: content}) / name)


def test_repro_yaml_maps_onto_the_cluster_spec(tmp_path: Path):
    directory = write(
        tmp_path / "r",
        {
            "repro.yaml": (
                "title: x\nshards: 2\nkeeper: 1\nwith: [s3, iceberg-rest]\nbuckets: [warehouse, other]\n"
                "users: ['dev:pw:readonly']\ngrants: ['dev:NAMED COLLECTION ON s3']\n"
                "query_settings: {allow_insert_into_iceberg: 1, use_cache: true}\n"
                "settings: {max_concurrent_queries: 200}\ns3_users: ['reader:readerpass:readonly:other']\n"
                "databases: ['db:Replicated']\nseed: true\n"
            ),
            "01.sql": "SELECT 1",
        },
    )
    spec = ClusterSpec(name="x", source="/x", **repro.load(directory).fields)
    assert (spec.shards, spec.effective_keeper_nodes, spec.seed) == (2, 1, True)
    assert spec.services == ("s3", "iceberg-rest")
    assert spec.users[0].grants == ("NAMED COLLECTION ON s3",)
    assert spec.profile_settings == {"allow_insert_into_iceberg": "1", "use_cache": "1"}
    assert spec.settings == {"max_concurrent_queries": "200"}
    assert spec.s3_users[0].buckets == ("other",)
    assert spec.databases[0].engine == "Replicated"


@pytest.mark.parametrize(
    "yaml_text, message",
    [
        ("titel: typo\n", "unknown key"),
        ("buckets: [x1y]\n", "--with s3"),
        ("grants: ['ghost:SELECT ON *.*']\n", "not in users"),
        ("- a list\n", "mapping"),
        ("shards: [\n", "not valid YAML"),
    ],
)
def test_bad_repro_yaml_fails_before_anything_launches(tmp_path: Path, yaml_text, message):
    directory = write(tmp_path / "r", {"repro.yaml": yaml_text, "01.sql": "SELECT 1"})
    with pytest.raises(Exception, match=message):
        repro.load(directory)


def test_a_directory_without_steps_is_rejected(tmp_path: Path):
    with pytest.raises(ReproError, match="no steps"):
        repro.load(minimal(tmp_path))


def test_a_directory_without_repro_yaml_says_how_to_start(tmp_path: Path):
    with pytest.raises(ReproError, match="chlab repro init"):
        repro.load(tmp_path)


# --- evaluation ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expect, exit_code, code, output, failures",
    [
        (Expectation(), 0, None, "x", 0),
        (Expectation(), 1, 60, "", 1),
        (Expectation(outcome="error"), 0, None, "", 1),
        (Expectation(outcome="error"), 1, 60, "", 0),
        (Expectation(outcome="error", code=60), 1, 60, "", 0),
        (Expectation(outcome="error", code=60), 1, 497, "", 1),
        (Expectation(contains=("ab",)), 0, None, "xaby", 0),
        (Expectation(contains=("ab", "zz")), 0, None, "xaby", 1),
        (Expectation(empty=True), 0, None, "  \n", 0),
        (Expectation(empty=True), 0, None, "x", 1),
        (Expectation(reference="2\n"), 0, None, "2", 0),
        (Expectation(reference="a  \nb\n"), 0, None, "a\nb\n\n", 0),
    ],
)
def test_evaluate(expect, exit_code, code, output, failures):
    assert len(evaluate(expect, exit_code, code, output)[0]) == failures


def test_a_reference_mismatch_comes_with_a_diff():
    failures, diff = evaluate(Expectation(reference="2\n"), 0, None, "3\n")
    assert failures == ["output differs from .reference"]
    assert "-2" in diff and "+3" in diff


# --- .chlab steps --------------------------------------------------------------------------


def test_chlab_lines_get_the_instance_inserted():
    command = repro._chlab_script(
        "lab",
        "# comment\nrestart --node clickhouse2\ns3 put ./x warehouse/\ncp ./x {instance}:/tmp/\n",
    )
    script = command[-1]
    assert script.startswith("set -e\n")
    assert f"{sys.executable} -m chlab.cli restart lab --node clickhouse2" in script
    assert "chlab.cli s3 put lab ./x warehouse/" in script
    assert "chlab.cli cp ./x lab:/tmp/" in script


@pytest.mark.parametrize("line", ["rm -y", "down", "up -c x", "ui"])
def test_chlab_steps_cannot_end_the_instance(line):
    with pytest.raises(ReproError, match="not allowed"):
        repro._chlab_script("lab", line)


def test_the_cli_runs_as_a_module():
    """.chlab steps call `python -m chlab.cli`; that entry point must exist."""
    result = subprocess.run(
        [sys.executable, "-m", "chlab.cli", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0 and "repro" in result.stdout


# --- running -------------------------------------------------------------------------------


class FakeRuntime:
    """Enough of Runtime for the runner: up/destroy recorded, commands echo their step."""

    def __init__(self, registry, fail_up: bool = False):
        from chlab.core.cache import Cache

        self.registry = registry
        self.cache = Cache(registry.root.parent / "cache")
        self.fail_up = fail_up
        self.destroyed: list[str] = []
        self.trusted: dict[str, bool] = {}

    def up(self, spec, trust_local=True, on_event=None):
        from chlab.core.runtime import ClusterError, InstanceStatus
        from chlab.core.topology import build_topology

        self.trusted[spec.source] = trust_local
        if self.fail_up:
            raise ClusterError("clickhouse1 did not become healthy")
        spec = spec.with_ports(21000).with_version("9.9.9." + spec.source[-1])
        self.registry.save(spec)
        return InstanceStatus(spec=spec, topology=build_topology(spec), services=())

    def destroy(self, name):
        self.destroyed.append(name)
        self.registry.delete(name)

    def client_script_command(self, name, **kwargs):
        return ["client", name]


@pytest.fixture
def registry(tmp_path: Path):
    from chlab.core.registry import Registry

    return Registry(tmp_path / "state")


def fake_subprocess(monkeypatch, behaviour):
    def run(command, input=None, cwd=None, env=None, timeout=None, **kwargs):
        exit_code, stdout, stderr = behaviour(command, input)
        return subprocess.CompletedProcess(command, exit_code, stdout, stderr)

    monkeypatch.setattr("chlab.core.repro.subprocess.run", run)


def test_runner_evaluates_each_step_and_removes_the_instance(tmp_path, registry, monkeypatch):
    directory = minimal(
        tmp_path,
        **{
            "01.sql": "SELECT 1",
            "01.reference": "1\n",
            "02.sql": "-- chlab: expect error 60\nSELECT nope",
        },
    )
    fake_subprocess(
        monkeypatch,
        lambda command, stdin: (
            (0, "1\n", "") if "SELECT 1" in stdin else (60, "", "Code: 60. DB::Exception: x")
        ),
    )
    runtime = FakeRuntime(registry)
    run = repro.Runner(runtime).run(repro.load(directory), ["/build/a"])
    assert run.passed
    assert [result.error_code for result in run.builds[0].steps] == [None, 60]
    assert runtime.destroyed == ["repro-r-1"]


def test_keep_leaves_the_instance(tmp_path, registry, monkeypatch):
    fake_subprocess(monkeypatch, lambda command, stdin: (0, "", ""))
    runtime = FakeRuntime(registry)
    run = repro.Runner(runtime).run(
        repro.load(minimal(tmp_path, **{"01.sql": "x"})), ["/b/a"], keep=True
    )
    assert run.builds[0].kept and runtime.destroyed == []


def test_a_launch_failure_is_recorded_not_raised(tmp_path, registry):
    run = repro.Runner(FakeRuntime(registry, fail_up=True)).run(
        repro.load(minimal(tmp_path, **{"01.sql": "x"})), ["/b/a"]
    )
    assert not run.launched and "did not become healthy" in run.builds[0].error


def test_compare_reports_the_steps_where_builds_differ(tmp_path, registry, monkeypatch):
    directory = minimal(tmp_path, **{"01.sql": "same", "02.sql": "differs"})

    def behaviour(command, stdin):
        instance = command[1]
        if stdin == "differs" and instance.endswith("-2"):
            return 0, "new\n", ""
        return 0, "old\n", ""

    fake_subprocess(monkeypatch, behaviour)
    run = repro.Runner(FakeRuntime(registry)).run(repro.load(directory), ["/b/a", "/b/b"])
    assert [build.instance for build in run.builds] == ["repro-r-1", "repro-r-2"]
    assert run.differences == ["02.sql"]
    report = repro.markdown(run)
    assert "Builds differ at: `02.sql`" in report


def test_a_timed_out_step_fails_with_124(tmp_path, registry, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 1, output=b"partial")

    monkeypatch.setattr("chlab.core.repro.subprocess.run", run)
    result = (
        repro.Runner(FakeRuntime(registry))
        .run(
            repro.load(minimal(tmp_path, **{"01.sql": "-- chlab: timeout 1\nSELECT sleep(3)"})),
            ["/b/a"],
        )
        .builds[0]
        .steps[0]
    )
    assert result.exit_code == 124 and "timed out" in result.error_output and not result.passed


def test_markdown_shows_failures_with_their_output(tmp_path, registry, monkeypatch):
    fake_subprocess(monkeypatch, lambda command, stdin: (0, "3\n", ""))
    run = repro.Runner(FakeRuntime(registry)).run(
        repro.load(minimal(tmp_path, **{"01.sql": "x", "01.reference": "2\n"})), ["/b/a"]
    )
    report = repro.markdown(run)
    assert "**FAILED**" in report and "```diff" in report and "+3" in report


def test_instance_names_are_valid_and_unique():
    taken = {"repro-my-repro-1"}
    assert repro._instance_name("My Repro!", 1, taken) == "repro-my-repro-2"
    assert repro._instance_name("x" * 80, 1, set()).startswith("repro-xxxx")


# --- CLI -----------------------------------------------------------------------------------


runner = CliRunner()


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CHLAB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHLAB_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path


def test_init_then_check(isolated: Path):
    directory = isolated / "new"
    assert runner.invoke(app, ["repro", "init", str(directory), "--with", "s3"]).exit_code == 0
    assert "with: [s3]" in (directory / "repro.yaml").read_text()
    result = runner.invoke(app, ["repro", "check", str(directory), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["steps"][0]["step"] == "01-setup.sql"
    assert runner.invoke(app, ["repro", "init", str(directory)]).exit_code == 1


def _stub_run(monkeypatch, run_result):
    monkeypatch.setattr(
        repro.Runner,
        "run",
        lambda self, loaded, sources, keep=False, untrusted=(): run_result(sources),
    )


def _run(sources, *, passed=True, error=None):
    builds = []
    for source in sources:
        build = repro.BuildRun(source=source, instance="i", version="1", error=error)
        if error is None:
            build.steps.append(
                repro.StepResult(
                    "01.sql",
                    "sql",
                    "first node",
                    "ok",
                    0 if passed else 1,
                    None,
                    "",
                    "",
                    0.1,
                    passed,
                )
            )
        builds.append(build)
    return repro.ReproRun(title="t", directory="d", started="2026-01-01T00:00:00Z", builds=builds)


@pytest.mark.parametrize(
    "kwargs, exit_code", [({"passed": True}, 0), ({"passed": False}, 1), ({"error": "boom"}, 2)]
)
def test_run_exit_codes(isolated: Path, monkeypatch, kwargs, exit_code):
    directory = minimal(isolated, **{"01.sql": "SELECT 1"})
    _stub_run(monkeypatch, lambda sources: _run(sources, **kwargs))
    out = isolated / "out"
    result = runner.invoke(app, ["repro", "run", str(directory), "-c", "/b/a", "--out", str(out)])
    assert result.exit_code == exit_code, result.output
    assert json.loads((out / "result.json").read_text())["passed"] is (exit_code == 0)
    assert (out / "report.md").read_text().startswith("# Reproduction: t")


def test_run_needs_a_build(isolated: Path):
    result = runner.invoke(app, ["repro", "run", str(minimal(isolated, **{"01.sql": "x"}))])
    assert result.exit_code == 1 and "-c" in result.output


def test_run_takes_builds_from_repro_yaml(isolated: Path, monkeypatch):
    directory = write(
        isolated / "r", {"repro.yaml": "clickhouse: /b/a\ncompare: /b/b\n", "01.sql": "x"}
    )
    seen = {}
    _stub_run(monkeypatch, lambda sources: seen.setdefault("s", sources) and _run(sources))
    assert (
        runner.invoke(app, ["repro", "run", str(directory), "--out", str(isolated / "o")]).exit_code
        == 0
    )
    assert seen["s"] == ["/b/a", "/b/b"]


def test_an_explicit_build_keeps_the_yaml_comparison(isolated: Path, monkeypatch):
    directory = write(
        isolated / "r", {"repro.yaml": "clickhouse: /b/a\ncompare: /b/b\n", "01.sql": "x"}
    )
    seen = {}
    _stub_run(monkeypatch, lambda sources: seen.setdefault("s", sources) and _run(sources))
    runner.invoke(
        app, ["repro", "run", str(directory), "-c", "/b/pr", "--out", str(isolated / "o")]
    )
    assert seen["s"] == ["/b/pr", "/b/b"]


def test_no_compare_ignores_the_yaml_comparison(isolated: Path, monkeypatch):
    directory = write(
        isolated / "r", {"repro.yaml": "clickhouse: /b/a\ncompare: /b/b\n", "01.sql": "x"}
    )
    seen = {}
    _stub_run(monkeypatch, lambda sources: seen.setdefault("s", sources) and _run(sources))
    runner.invoke(
        app, ["repro", "run", str(directory), "--no-compare", "--out", str(isolated / "o")]
    )
    assert seen["s"] == ["/b/a"]


def test_chlab_steps_see_the_runners_state_directory(tmp_path, registry, monkeypatch):
    seen = {}

    def run(command, input=None, cwd=None, env=None, **kwargs):
        seen["env"] = env
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("chlab.core.repro.subprocess.run", run)
    repro.Runner(FakeRuntime(registry)).run(
        repro.load(minimal(tmp_path, **{"01.chlab": "restart"})), ["/b/a"]
    )
    assert seen["env"]["CHLAB_STATE_DIR"] == str(registry.root)
