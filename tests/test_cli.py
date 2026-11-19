"""CLI behaviour: argument parsing, output shapes, and the error path.

Docker is never reached — the Runtime methods these commands call are stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chlab.cli import app
from chlab.core.registry import Registry
from chlab.core.runtime import Runtime
from chlab.core.spec import ClusterSpec, UserSpec

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch):
    """Point every on-disk location at a tmpdir so tests never touch real state."""
    monkeypatch.setenv("CHLAB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHLAB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CHLAB_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    return tmp_path


@pytest.fixture
def saved_instance(isolated_state: Path) -> ClusterSpec:
    spec = ClusterSpec(
        name="lab",
        source="/bin/clickhouse",
        version="24.8.1.1",
        shards=2,
        replicas=1,
        port_base=21000,
    )
    Registry().save(spec)
    return spec


def invoke(*args):
    return runner.invoke(app, list(args))


# --- argument parsing --------------------------------------------------------------------


def captured_spec(monkeypatch) -> dict:
    """Capture the ClusterSpec `up` builds, without launching anything."""
    seen: dict = {}

    def fake_up(self, spec, **kwargs):
        seen["spec"] = spec
        seen["kwargs"] = kwargs
        return self.status(spec.name)

    monkeypatch.setattr(Runtime, "up", fake_up)
    monkeypatch.setattr(Runtime, "status", lambda self, name: _stub_status(name))
    return seen


def _stub_status(name: str):
    from chlab.core.runtime import InstanceStatus
    from chlab.core.topology import build_topology

    spec = (
        Registry().load(name)
        if Registry().exists(name)
        else ClusterSpec(name=name, source="/x", port_base=21000)
    )
    return InstanceStatus(spec=spec, topology=build_topology(spec), services=())


def test_up_builds_the_requested_topology(monkeypatch):
    seen = captured_spec(monkeypatch)
    result = invoke("up", "-c", "/bin/clickhouse", "-n", "lab", "-s", "3", "-r", "2", "-k", "3")
    assert result.exit_code == 0, result.output
    spec = seen["spec"]
    assert (spec.shards, spec.replicas, spec.keeper_nodes) == (3, 2, 3)


def test_up_parses_users_roles_and_grants(monkeypatch):
    seen = captured_spec(monkeypatch)
    invoke(
        "up",
        "-c",
        "/x",
        "-n",
        "lab",
        "-u",
        "dev:pw",
        "-u",
        "ro:pw:readonly",
        "-u",
        "boss:pw:admin",
        "--grant",
        "ro:SELECT ON demo.*",
    )
    users = {user.name: user for user in seen["spec"].users}
    assert users["dev"].role == "readwrite"
    assert users["ro"].role == "readonly"
    assert users["boss"].is_admin
    assert users["ro"].grants == ("SELECT ON demo.*",)


def test_grant_for_an_unknown_user_is_refused(monkeypatch):
    captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "lab", "-u", "dev", "--grant", "nobody:SELECT ON *.*")
    assert result.exit_code == 1
    assert "not created with --user" in result.output


def test_grant_without_any_user_is_refused(monkeypatch):
    captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "lab", "--grant", "dev:SELECT ON *.*")
    assert result.exit_code == 1
    assert "--grant needs a matching --user" in result.output


def test_settings_are_validated_before_launch(monkeypatch):
    """An invalid key would otherwise produce malformed XML and a 180-second health timeout."""
    captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "lab", "--setting", "max threads=4")
    assert result.exit_code == 1
    assert "invalid setting name" in result.output


def test_settings_are_parsed(monkeypatch):
    seen = captured_spec(monkeypatch)
    invoke("up", "-c", "/x", "-n", "lab", "--setting", "max_concurrent_queries=7")
    assert seen["spec"].settings == {"max_concurrent_queries": "7"}


def test_no_seed_overrides_a_profile(monkeypatch):
    """A profile saved with seed:true must not seed forever."""
    from chlab.core.profiles import Profiles

    Profiles.default().save("seeded", ClusterSpec(name="p", source="/x", seed=True))
    seen = captured_spec(monkeypatch)

    invoke("up", "-c", "/x", "-n", "a", "-p", "seeded")
    assert seen["spec"].seed is True

    invoke("up", "-c", "/x", "-n", "b", "-p", "seeded", "--no-seed")
    assert seen["spec"].seed is False


def test_a_profile_with_unknown_fields_still_launches(monkeypatch, isolated_state: Path):
    """A profile written by a newer chlab must not traceback."""
    from chlab.core.profiles import Profiles

    profiles = Profiles.default()
    profiles.save("future", ClusterSpec(name="p", source="/x", shards=2))
    raw = json.loads(profiles.path.read_text())
    raw["future"]["some_new_field"] = {"nested": True}
    profiles.path.write_text(json.dumps(raw))

    seen = captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "lab", "-p", "future")
    assert result.exit_code == 0, result.output
    assert seen["spec"].shards == 2


def test_name_is_derived_when_omitted(monkeypatch):
    seen = captured_spec(monkeypatch)
    invoke("up", "-c", "docker://x/y:26.3.12.20001")
    assert seen["spec"].name == "ch-26-3-12-20001"


def test_recreate_is_passed_through(monkeypatch):
    seen = captured_spec(monkeypatch)
    invoke("up", "-c", "/x", "-n", "lab", "--recreate")
    assert seen["kwargs"]["recreate"] is True


# --- output shapes -----------------------------------------------------------------------


def test_ls_reports_nothing_when_empty():
    result = invoke("ls")
    assert result.exit_code == 0
    assert "no instances" in result.output


def test_ls_json_is_a_list(saved_instance):
    result = invoke("ls", "--json")
    assert result.exit_code == 0
    assert [entry["name"] for entry in json.loads(result.output)] == ["lab"]


def test_status_json_describes_nodes(saved_instance):
    payload = json.loads(invoke("status", "lab", "--json").output)
    assert payload["version"] == "24.8.1.1"
    assert [node["name"] for node in payload["nodes"]] == ["clickhouse1", "clickhouse2"]
    assert payload["nodes"][0]["play"].endswith("/play")


def test_status_shows_users_and_play_urls(isolated_state: Path):
    Registry().save(
        ClusterSpec(
            name="lab", source="/x", port_base=21000, users=(UserSpec("dev", "pw", role="admin"),)
        )
    )
    output = invoke("status", "lab").output
    assert "dev (admin)" in output
    assert "/play" in output


def test_state_is_unknown_when_docker_cannot_be_reached(saved_instance, monkeypatch):
    """Reporting "created" would claim the containers are gone when we simply could not ask."""
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: None)
    assert json.loads(invoke("status", "lab", "--json").output)["state"] == "unknown"


# --- error path --------------------------------------------------------------------------


def test_unknown_instance_exits_nonzero_without_a_traceback():
    result = invoke("status", "ghost")
    assert result.exit_code == 1
    assert "no instance named 'ghost'" in result.output
    assert "Traceback" not in result.output


def test_missing_source_file_is_reported_cleanly():
    result = invoke("resolve", "/nonexistent/clickhouse")
    assert result.exit_code == 1
    assert "does not exist" in result.output
    assert "Traceback" not in result.output


def test_corrupt_spec_is_reported_with_its_path(isolated_state: Path):
    """The instance must stay removable rather than tracebacking on every command."""
    directory = Registry().instance_dir("broken")
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text("{truncated")

    result = invoke("status", "broken")
    assert result.exit_code == 1
    assert "not readable JSON" in result.output
    assert "Traceback" not in result.output


def test_bad_instance_name_is_reported(monkeypatch):
    captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "Bad Name")
    assert result.exit_code == 1
    assert "lowercase" in result.output


def test_profiles_round_trip_through_the_cli(saved_instance):
    assert invoke("profiles", "save", "two-shard", "lab").exit_code == 0
    assert "two-shard" in invoke("profiles", "ls").output
    assert invoke("profiles", "rm", "two-shard").exit_code == 0
    assert "no profiles" in invoke("profiles", "ls").output


def test_removing_an_unknown_profile_is_reported():
    result = invoke("profiles", "rm", "ghost")
    assert result.exit_code == 1
    assert "no profile named 'ghost'" in result.output


def test_cache_ls_is_empty_initially():
    assert "nothing resolved yet" in invoke("cache", "ls").output


def test_cache_rm_reports_a_miss():
    result = invoke("cache", "rm", "24.8")
    assert result.exit_code == 1
    assert "nothing cached matches" in result.output


# --- reproduction primitives -------------------------------------------------------------


def test_query_settings_at_launch_go_into_the_profile(monkeypatch):
    seen = captured_spec(monkeypatch)
    result = invoke("up", "-c", "/x", "-n", "lab", "--query-setting", "allow_insert_into_iceberg=1")
    assert result.exit_code == 0, result.output
    assert seen["spec"].profile_settings == {"allow_insert_into_iceberg": "1"}
    assert seen["spec"].settings == {}


def test_query_failure_reports_the_error_code_as_json_and_exits_1(saved_instance, monkeypatch):
    from chlab.core.query import QueryError

    def fail(self, name, sql, **kwargs):
        raise QueryError("Code: 60. DB::Exception: Unknown table", node="clickhouse1")

    monkeypatch.setattr(Runtime, "query", fail)
    result = invoke("query", "lab", "SELECT * FROM nope", "--json")
    assert result.exit_code == 1
    assert json.loads(result.stdout) == [
        {"node": "clickhouse1", "error": "Code: 60. DB::Exception: Unknown table", "code": 60}
    ]


def test_query_passes_settings_through(saved_instance, monkeypatch):
    from chlab.core.query import QueryResult

    seen = {}

    def capture(self, name, sql, **kwargs):
        seen.update(kwargs)
        return QueryResult(node="clickhouse1")

    monkeypatch.setattr(Runtime, "query", capture)
    result = invoke("query", "lab", "SELECT 1", "-s", "max_threads=1", "--setting", "a.b=2")
    assert result.exit_code == 0, result.output
    assert seen["settings"] == {"max_threads": "1", "a.b": "2"}


def test_fan_out_exits_1_when_any_node_fails(saved_instance, monkeypatch):
    from chlab.core.query import QueryError, QueryResult

    monkeypatch.setattr(
        Runtime,
        "query_all",
        lambda self, name, sql, **kw: [
            QueryResult(columns=["x"], rows=[[1]], node="clickhouse1"),
            QueryError("Code: 497. ACCESS_DENIED", node="clickhouse2"),
        ],
    )
    result = invoke("query", "lab", "SELECT 1", "--all")
    assert result.exit_code == 1


def test_server_errors_with_brackets_are_printed_verbatim(saved_instance, monkeypatch):
    from chlab.core.query import QueryError

    def fail(self, name, sql, **kwargs):
        raise QueryError("Code: 47. Unknown identifier [bold]x[/bold]", node="clickhouse1")

    monkeypatch.setattr(Runtime, "query", fail)
    result = invoke("query", "lab", "SELECT x")
    assert "[bold]x[/bold]" in result.output


@pytest.mark.parametrize("args", [[], ["SELECT 1", "--file", "x.sql"]])
def test_query_needs_exactly_one_of_sql_or_file(saved_instance, args):
    result = invoke("query", "lab", *args)
    assert result.exit_code == 1
    assert "exactly one" in result.output


def test_query_file_runs_through_the_client_on_every_node(saved_instance, monkeypatch, tmp_path):
    script = tmp_path / "steps.sql"
    script.write_text("SELECT 1; SELECT 2;")
    runs = []

    class Done:
        returncode = 0

    def fake_run(command, input, text):
        runs.append((command, input))
        return Done()

    monkeypatch.setattr("subprocess.run", fake_run)
    result = invoke("query", "lab", "--file", str(script), "--all")
    assert result.exit_code == 0, result.output
    assert [command[3] for command, _ in runs] == ["chlab-lab-clickhouse1", "chlab-lab-clickhouse2"]
    assert all(stdin == "SELECT 1; SELECT 2;" for _, stdin in runs)


def test_query_file_propagates_the_client_exit_code(saved_instance, monkeypatch, tmp_path):
    script = tmp_path / "steps.sql"
    script.write_text("SELEKT 1")

    class Failed:
        returncode = 62

    monkeypatch.setattr("subprocess.run", lambda *a, **kw: Failed())
    assert invoke("query", "lab", "-f", str(script)).exit_code == 62


def test_exec_returns_the_command_exit_code(saved_instance, monkeypatch):
    seen = {}

    def fake_call(command):
        seen["command"] = command
        return 3

    monkeypatch.setattr("subprocess.call", fake_call)
    result = invoke("exec", "lab", "--node", "clickhouse2", "--", "ls", "-la", "/")
    assert result.exit_code == 3
    assert seen["command"][-4:] == ["chlab-lab-clickhouse2", "ls", "-la", "/"]


def test_exec_reports_an_unknown_node(saved_instance):
    result = invoke("exec", "lab", "--node", "clickhouse9", "--", "true")
    assert result.exit_code == 1
    assert "clickhouse9" in result.output


@pytest.mark.parametrize(
    "source, destination, expected",
    [
        ("./x.parquet", "lab:/tmp/", ["./x.parquet", "chlab-lab-clickhouse1:/tmp/"]),
        ("lab/clickhouse2:/tmp/x", "./x", ["chlab-lab-clickhouse2:/tmp/x", "./x"]),
    ],
)
def test_cp_maps_instance_paths_to_containers(
    saved_instance, monkeypatch, source, destination, expected
):
    seen = {}
    monkeypatch.setattr("subprocess.call", lambda command: seen.setdefault("c", command) and 0)
    result = invoke("cp", source, destination)
    assert result.exit_code == 0, result.output
    assert seen["c"] == ["docker", "cp", *expected]


@pytest.mark.parametrize(
    "source, destination", [("./a", "./b"), ("lab:/a", "lab:/b"), ("nope:/a", "./b")]
)
def test_cp_needs_exactly_one_instance_side(saved_instance, source, destination):
    assert invoke("cp", source, destination).exit_code == 1


def test_env_json_describes_how_to_reach_the_instance(saved_instance):
    result = invoke("env", "lab", "--json")
    assert result.exit_code == 0, result.output
    info = json.loads(result.stdout)
    assert [node["container"] for node in info["nodes"]] == [
        "chlab-lab-clickhouse1",
        "chlab-lab-clickhouse2",
    ]
    assert info["network"] == "chlab-lab_default"


def test_env_reports_an_unknown_instance():
    assert invoke("env", "nope").exit_code == 1
