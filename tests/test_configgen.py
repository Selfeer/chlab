from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from chlab.core import configgen
from chlab.core.spec import ClusterSpec
from chlab.core.topology import build_topology


def topology(shards=2, replicas=2, keeper_nodes=1, **overrides):
    return build_topology(
        ClusterSpec(
            name="t",
            source="/bin/clickhouse",
            shards=shards,
            replicas=replicas,
            keeper_nodes=keeper_nodes,
            port_base=21000,
            **overrides,
        )
    )


def parse(document: dict) -> ET.Element:
    return ET.fromstring(configgen.to_xml(document))


def test_remote_servers_lists_every_replica_under_its_shard():
    root = parse(configgen.remote_servers(topology()))
    cluster = root.find("remote_servers/default_cluster")
    shards = cluster.findall("shard")
    assert len(shards) == 2
    hosts = [[replica.find("host").text for replica in shard.findall("replica")] for shard in shards]
    assert hosts == [["clickhouse1", "clickhouse2"], ["clickhouse3", "clickhouse4"]]


def test_remote_servers_enables_internal_replication():
    root = parse(configgen.remote_servers(topology()))
    assert root.find("remote_servers/default_cluster/shard/internal_replication").text == "true"


def test_cluster_name_is_configurable():
    root = parse(configgen.remote_servers(topology(cluster_name="my_cluster")))
    assert root.find("remote_servers/my_cluster") is not None


def test_macros_identify_the_node_within_the_cluster():
    result = topology()
    root = parse(configgen.macros(result.spec, result.nodes[2]))
    assert root.find("macros/shard").text == "2"
    assert root.find("macros/replica").text == "clickhouse3"


def test_zookeeper_points_at_every_keeper():
    root = parse(configgen.zookeeper(topology(keeper_nodes=3).keepers))
    hosts = [node.find("host").text for node in root.findall("zookeeper/node")]
    assert hosts == ["keeper1", "keeper2", "keeper3"]


def test_keeper_config_declares_the_whole_raft_group():
    result = topology(keeper_nodes=3)
    root = parse(configgen.keeper_config(result.keepers[1], result.keepers))
    assert root.find("keeper_server/server_id").text == "2"
    servers = root.findall("keeper_server/raft_configuration/server")
    assert [server.find("id").text for server in servers] == ["1", "2", "3"]
    assert [server.find("hostname").text for server in servers] == ["keeper1", "keeper2", "keeper3"]


def test_base_config_listens_externally_and_keeps_sql_users():
    root = parse(configgen.base_config())
    assert root.find("listen_host").text == "0.0.0.0"
    # SQL-created users need somewhere to persist.
    assert root.find("user_directories/local_directory/path") is not None


def test_base_users_has_a_single_default_user():
    root = parse(configgen.base_users())
    assert [user.tag for user in root.find("users")] == ["default"]
    assert root.find("users/default/access_management").text == "1"


def test_list_values_repeat_their_tag():
    root = parse({"zookeeper": {"node": [{"host": "a"}, {"host": "b"}]}})
    assert [node.find("host").text for node in root.findall("zookeeper/node")] == ["a", "b"]


# --- file layout -------------------------------------------------------------------------


def test_base_image_mode_writes_a_complete_configuration(tmp_path: Path):
    configgen.write_configs(topology(), tmp_path, base_image_mode=True)
    node_dir = tmp_path / "configs" / "clickhouse1"
    assert (node_dir / "config.xml").is_file()
    assert (node_dir / "users.xml").is_file()
    assert (node_dir / "config.d" / "10-chlab-cluster.xml").is_file()
    assert (node_dir / "config.d" / "20-chlab-macros.xml").is_file()
    assert (node_dir / "config.d" / "30-chlab-keeper.xml").is_file()
    assert (node_dir / "users.d" / "10-chlab-default.xml").is_file()


def test_docker_image_mode_writes_only_overlays(tmp_path: Path):
    """Stock images ship their own config.xml; overwriting it would change what we are testing."""
    configgen.write_configs(topology(), tmp_path, base_image_mode=False)
    node_dir = tmp_path / "configs" / "clickhouse1"
    assert not (node_dir / "config.xml").exists()
    assert not (node_dir / "users.xml").exists()
    assert (node_dir / "config.d" / "10-chlab-cluster.xml").is_file()


def test_keeper_config_is_written_per_keeper(tmp_path: Path):
    configgen.write_configs(topology(keeper_nodes=3), tmp_path, base_image_mode=True)
    for index in (1, 2, 3):
        assert (tmp_path / "configs" / f"keeper{index}" / "keeper_config.xml").is_file()


def test_no_keeper_config_without_keepers(tmp_path: Path):
    configgen.write_configs(
        topology(shards=1, replicas=1, keeper_nodes=0), tmp_path, base_image_mode=True
    )
    assert not (tmp_path / "configs" / "clickhouse1" / "config.d" / "30-chlab-keeper.xml").exists()


def test_custom_settings_are_written(tmp_path: Path):
    configgen.write_configs(
        topology(settings={"max_server_memory_usage": "4000000000"}),
        tmp_path,
        base_image_mode=True,
    )
    path = tmp_path / "configs" / "clickhouse1" / "config.d" / "50-chlab-settings.xml"
    assert "max_server_memory_usage" in path.read_text()
