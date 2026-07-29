from __future__ import annotations

from pathlib import Path

import yaml

from chlab.core import compose, configgen
from chlab.core.resolve import Resolved
from chlab.core.sources import ArtifactType, Source, SourceKind
from chlab.core.spec import ClusterSpec
from chlab.core.topology import build_topology


def binary_resolved(tmp_path: Path) -> Resolved:
    binary = tmp_path / "clickhouse"
    binary.write_bytes(b"")
    return Resolved(
        source=Source("/x", SourceKind.PATH, "/x", ArtifactType.BINARY),
        version="24.8.1.1",
        binary_path=binary,
    )


def docker_resolved() -> Resolved:
    return Resolved(
        source=Source("docker://i:t", SourceKind.DOCKER, "i:t", ArtifactType.IMAGE),
        version="24.8.1.1",
        docker_image="i:t",
    )


def topology(shards=2, replicas=2, keeper_nodes=1):
    return build_topology(
        ClusterSpec(
            name="lab",
            source="/x",
            shards=shards,
            replicas=replicas,
            keeper_nodes=keeper_nodes,
            port_base=21000,
        )
    )


def render(tmp_path: Path, resolved: Resolved, **kwargs) -> dict:
    result = topology(**kwargs)
    configgen.write_configs(result, tmp_path, base_image_mode=not resolved.runs_as_image)
    return compose.render(result, resolved, tmp_path, "some/image:tag")


def test_every_node_and_keeper_becomes_a_service(tmp_path: Path):
    document = render(tmp_path, binary_resolved(tmp_path))
    assert set(document["services"]) == {
        "clickhouse1",
        "clickhouse2",
        "clickhouse3",
        "clickhouse4",
        "keeper1",
    }


def test_base_image_mode_mounts_the_binary_and_owns_the_config(tmp_path: Path):
    resolved = binary_resolved(tmp_path)
    service = render(tmp_path, resolved)["services"]["clickhouse1"]
    mounts = " ".join(service["volumes"])
    assert f"{resolved.binary_path}:/usr/bin/clickhouse:ro" in mounts
    assert "/etc/clickhouse-server/config.xml:ro" in mounts
    assert service["entrypoint"][:2] == ["/usr/bin/clickhouse", "server"]


def test_docker_image_mode_touches_neither_binary_nor_entrypoint(tmp_path: Path):
    """The point of a docker:// source is to run that image as its authors shipped it."""
    service = render(tmp_path, docker_resolved())["services"]["clickhouse1"]
    mounts = " ".join(service["volumes"])
    assert "/usr/bin/clickhouse:ro" not in mounts
    assert "/etc/clickhouse-server/config.xml:ro" not in mounts
    assert "entrypoint" not in service


def test_overlays_are_mounted_file_by_file(tmp_path: Path):
    """Mounting config.d as a directory would hide the image's own listen configuration."""
    service = render(tmp_path, docker_resolved())["services"]["clickhouse1"]
    overlays = [mount for mount in service["volumes"] if "/etc/clickhouse-server/config.d" in mount]
    assert overlays, "expected config.d overlays"
    assert all(mount.split(":")[1].endswith(".xml") for mount in overlays)


def test_ports_are_bound_to_loopback_only(tmp_path: Path):
    """A local lab should not expose an unauthenticated server to the network."""
    document = render(tmp_path, binary_resolved(tmp_path))
    for service in document["services"].values():
        for mapping in service.get("ports", []):
            assert mapping.startswith("127.0.0.1:")


def test_node_ports_match_the_topology(tmp_path: Path):
    service = render(tmp_path, binary_resolved(tmp_path))["services"]["clickhouse2"]
    assert service["ports"] == ["127.0.0.1:21010:8123", "127.0.0.1:21011:9000"]


def test_keeper_runs_the_keeper_entrypoint_in_both_modes(tmp_path: Path):
    for resolved in (binary_resolved(tmp_path), docker_resolved()):
        service = render(tmp_path, resolved)["services"]["keeper1"]
        assert service["entrypoint"][:2] == ["/usr/bin/clickhouse", "keeper"]


def test_nodes_depend_on_keeper(tmp_path: Path):
    document = render(tmp_path, binary_resolved(tmp_path))
    assert document["services"]["clickhouse1"]["depends_on"] == ["keeper1"]


def test_no_keeper_dependency_when_there_is_no_keeper(tmp_path: Path):
    document = render(tmp_path, binary_resolved(tmp_path), shards=1, replicas=1, keeper_nodes=0)
    assert "depends_on" not in document["services"]["clickhouse1"]


def test_each_service_gets_its_own_data_volume(tmp_path: Path):
    document = render(tmp_path, binary_resolved(tmp_path))
    assert set(document["volumes"]) == {
        "clickhouse1-data",
        "clickhouse2-data",
        "clickhouse3-data",
        "clickhouse4-data",
        "keeper1-data",
    }


def test_container_names_are_namespaced_by_instance(tmp_path: Path):
    document = render(tmp_path, binary_resolved(tmp_path))
    assert document["services"]["clickhouse1"]["container_name"] == "chlab-lab-clickhouse1"


def test_written_file_is_valid_yaml(tmp_path: Path):
    resolved = binary_resolved(tmp_path)
    result = topology()
    configgen.write_configs(result, tmp_path, base_image_mode=True)
    path = compose.write(result, resolved, tmp_path, "image:tag")
    assert yaml.safe_load(path.read_text())["services"]["clickhouse1"]["image"] == "image:tag"
