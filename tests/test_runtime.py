"""Lifecycle command construction, with Docker stubbed out."""

from __future__ import annotations

from pathlib import Path

import pytest

from chlab.core.registry import Registry
from chlab.core.runtime import Runtime, _parse_compose_ps
from chlab.core.spec import ClusterSpec


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch):
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    runtime.registry.save(
        ClusterSpec(name="lab", source="/bin/clickhouse", shards=2, replicas=2, port_base=21000)
    )
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(Runtime, "_compose", lambda self, spec, *args, **kw: calls.append(args))
    monkeypatch.setattr(Runtime, "_wait_healthy", lambda self, topology, **kw: None)
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    return runtime, calls


def test_start_recreates_missing_containers(runtime):
    """`compose start` fails outright if a container was removed, stranding the instance."""
    instance, calls = runtime
    instance.start("lab")
    assert calls[0][:2] == ("up", "-d")


def test_stop_keeps_containers(runtime):
    instance, calls = runtime
    instance.stop("lab")
    assert calls == [("stop",)]


def test_down_keeps_data_volumes(runtime):
    instance, calls = runtime
    instance.down("lab")
    assert "--volumes" not in calls[0]


def test_destroy_removes_volumes_and_the_definition(runtime):
    instance, calls = runtime
    instance.destroy("lab")
    assert "--volumes" in calls[0]
    assert not instance.registry.exists("lab")


def test_compose_command_targets_the_right_project(tmp_path: Path):
    instance = Runtime(registry=Registry(tmp_path / "state"))
    spec = ClusterSpec(name="lab", source="/x", port_base=21000)
    instance.registry.save(spec)
    command = instance._compose_command(spec, "ps")
    assert "--project-name" in command
    assert command[command.index("--project-name") + 1] == "chlab-lab"
    assert command[-1] == "ps"


def test_client_command_targets_the_named_container(tmp_path: Path):
    instance = Runtime(registry=Registry(tmp_path / "state"))
    instance.registry.save(
        ClusterSpec(name="lab", source="/x", shards=2, replicas=1, port_base=21000)
    )
    assert instance.client_command("lab")[:4] == ["docker", "exec", "-it", "chlab-lab-clickhouse1"]
    assert instance.client_command("lab", "clickhouse2")[3] == "chlab-lab-clickhouse2"


@pytest.mark.parametrize(
    "output, expected",
    [
        ('{"Service": "a", "State": "running"}\n{"Service": "b", "State": "exited"}', 2),
        ('[{"Service": "a", "State": "running"}]', 1),
        ("", 0),
        ("not json at all", 0),
    ],
)
def test_compose_ps_accepts_both_output_shapes(output, expected):
    """Compose has emitted both newline-delimited objects and a JSON array."""
    assert len(_parse_compose_ps(output)) == expected


def test_recreate_keeps_the_existing_ports(tmp_path: Path, monkeypatch):
    """Otherwise every bookmarked /play URL silently points at nothing after a recreate."""
    from chlab.core.resolve import Resolved
    from chlab.core.sources import ArtifactType, Source, SourceKind

    registry = Registry(tmp_path / "state")
    registry.save(ClusterSpec(name="lab", source="/bin/clickhouse", port_base=21600))
    instance = Runtime(registry=registry)

    monkeypatch.setattr(Runtime, "_compose", lambda self, spec, *a, **k: None)
    monkeypatch.setattr(Runtime, "_wait_healthy", lambda self, topology, **k: None)
    monkeypatch.setattr(Runtime, "_initialise", lambda self, topology, **k: None)
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    monkeypatch.setattr("chlab.core.runtime.ensure_base_image", lambda: "chlab/base:test")
    monkeypatch.setattr(
        "chlab.core.runtime.resolve",
        lambda *a, **k: Resolved(
            source=Source("/x", SourceKind.PATH, "/x", ArtifactType.BINARY),
            version="24.8.1.1",
            binary_path=tmp_path / "clickhouse",
        ),
    )
    (tmp_path / "clickhouse").write_bytes(b"")

    status = instance.up(ClusterSpec(name="lab", source="/bin/clickhouse"), recreate=True)
    assert status.spec.port_base == 21600


def test_recreate_allocates_ports_when_the_old_spec_is_unreadable(tmp_path: Path, monkeypatch):
    registry = Registry(tmp_path / "state")
    directory = registry.instance_dir("lab")
    directory.mkdir(parents=True)
    (directory / "spec.json").write_text("{truncated")
    instance = Runtime(registry=registry)

    spec = instance._inherit_ports(ClusterSpec(name="lab", source="/x"), lambda _m: None)
    assert spec.port_base is None  # falls through to a fresh allocation rather than raising


def test_restart_targets_only_the_named_services(runtime):
    instance, calls = runtime
    instance.restart("lab", ["clickhouse2"])
    assert calls == [("restart", "clickhouse2")]


def test_restart_without_services_restarts_everything(runtime):
    instance, calls = runtime
    instance.restart("lab")
    assert calls == [("restart",)]


def test_restart_rejects_unknown_services(runtime):
    from chlab.core.topology import UnknownNode

    instance, calls = runtime
    with pytest.raises(UnknownNode, match="clickhouse9"):
        instance.restart("lab", ["clickhouse9"])
    assert calls == []


def test_exec_command_runs_in_the_named_container(runtime):
    instance, _ = runtime
    command = instance.exec_command("lab", ["jq", "."], service="clickhouse3", interactive=True)
    assert command == ["docker", "exec", "-i", "chlab-lab-clickhouse3", "jq", "."]


def test_exec_can_target_a_keeper(tmp_path: Path):
    instance = Runtime(registry=Registry(tmp_path / "state"))
    instance.registry.save(ClusterSpec(name="lab", source="/x", replicas=2, port_base=21000))
    assert instance.exec_command("lab", ["true"], service="keeper1")[2] == "chlab-lab-keeper1"


def test_copy_command_goes_both_ways(runtime):
    instance, _ = runtime
    into = instance.copy_command(
        "lab", service=None, container_path="/tmp/x", host_path="./x", to_container=True
    )
    out = instance.copy_command(
        "lab", service="clickhouse2", container_path="/tmp/x", host_path="./x", to_container=False
    )
    assert into == ["docker", "cp", "./x", "chlab-lab-clickhouse1:/tmp/x"]
    assert out == ["docker", "cp", "chlab-lab-clickhouse2:/tmp/x", "./x"]


def test_client_script_command_runs_multiquery_with_settings(runtime):
    instance, _ = runtime
    command = instance.client_script_command(
        "lab",
        node="clickhouse2",
        user="dev",
        password="pw",
        settings={"allow_insert_into_iceberg": "1"},
        output_format="TSV",
    )
    assert command[:4] == ["docker", "exec", "-i", "chlab-lab-clickhouse2"]
    assert "--multiquery" in command
    assert command[command.index("--user") + 1] == "dev"
    assert command[command.index("--password") + 1] == "pw"
    assert command[command.index("--format") + 1] == "TSV"
    assert "--allow_insert_into_iceberg=1" in command


def test_environment_reports_host_and_network_endpoints(runtime):
    from chlab.core.environment import describe

    instance, _ = runtime
    info = describe(instance.status("lab"), instance.registry)
    first = info["nodes"][0]
    assert first["container"] == "chlab-lab-clickhouse1"
    assert first["host"]["http"] == "http://127.0.0.1:21000"
    assert first["network"]["http"] == "http://clickhouse1:8123"
    assert info["network"] == "chlab-lab_default"
    assert info["keepers"][0]["network"] == "keeper1:9181"
    assert info["users"][0]["name"] == "default"


def test_initialise_creates_catalog_databases_with_their_setting(tmp_path: Path, monkeypatch):
    from chlab.core.spec import DatabaseSpec
    from chlab.core.topology import build_topology

    calls = []
    monkeypatch.setattr("chlab.core.runtime.companions.active", lambda topology: iter(()))
    monkeypatch.setattr(
        "chlab.core.runtime.execute",
        lambda url, sql, **kwargs: calls.append((kwargs["node"], sql, kwargs.get("settings"))),
    )
    topology = build_topology(
        ClusterSpec(
            name="lab",
            source="/x",
            shards=2,
            services=("s3", "iceberg-rest"),
            databases=(DatabaseSpec("rest", "iceberg-rest"),),
            version="26.8.1.1",
            port_base=21600,
        )
    )
    Runtime(registry=Registry(tmp_path / "state"))._initialise(topology, notify=lambda _m: None)

    assert [node for node, _sql, _settings in calls] == ["clickhouse1", "clickhouse2"]
    for _node, sql, settings in calls:
        assert "ENGINE = DataLakeCatalog(" in sql
        assert settings == {"allow_experimental_database_iceberg": "1"}
