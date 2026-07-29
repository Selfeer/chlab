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
    monkeypatch.setattr(Runtime, "_services", lambda self, spec: [])
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
