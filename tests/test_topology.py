from __future__ import annotations

import pytest

from chlab.core.ports import allocate_block, is_free
from chlab.core.spec import ClusterSpec
from chlab.core.topology import UnknownNode, build_topology, port_span


def topology(shards=1, replicas=1, keeper_nodes=None, port_base=21000):
    return build_topology(
        ClusterSpec(
            name="t",
            source="/bin/clickhouse",
            shards=shards,
            replicas=replicas,
            keeper_nodes=keeper_nodes,
            port_base=port_base,
        )
    )


def test_nodes_fill_shard_by_shard():
    """clickhouse1/2 are the replicas of shard 1, clickhouse3/4 of shard 2."""
    layout = [
        (node.name, node.shard, node.replica) for node in topology(shards=2, replicas=2).nodes
    ]
    assert layout == [
        ("clickhouse1", 1, 1),
        ("clickhouse2", 1, 2),
        ("clickhouse3", 2, 1),
        ("clickhouse4", 2, 2),
    ]


def test_shard_layout_groups_replicas():
    grouped = [
        [node.name for node in shard] for shard in topology(shards=2, replicas=2).shard_layout()
    ]
    assert grouped == [["clickhouse1", "clickhouse2"], ["clickhouse3", "clickhouse4"]]


def test_ports_are_distinct_and_derived_from_the_base():
    nodes = topology(shards=2, replicas=2, port_base=21000).nodes
    assert [node.http_port for node in nodes] == [21000, 21010, 21020, 21030]
    assert [node.tcp_port for node in nodes] == [21001, 21011, 21021, 21031]
    ports = [port for node in nodes for port in (node.http_port, node.tcp_port)]
    assert len(set(ports)) == len(ports)


def test_keeper_ports_do_not_collide_with_node_ports():
    result = topology(shards=3, replicas=2, keeper_nodes=3, port_base=21000)
    node_ports = {port for node in result.nodes for port in (node.http_port, node.tcp_port)}
    keeper_ports = {keeper.client_port for keeper in result.keepers}
    assert not node_ports & keeper_ports


def test_keeper_ids_are_one_based():
    assert [keeper.index for keeper in topology(keeper_nodes=3).keepers] == [1, 2, 3]


def test_play_url_is_the_node_http_url():
    node = topology().nodes[0]
    assert node.play_url == f"{node.http_url}/play"


def test_port_span_covers_both_nodes_and_keepers():
    spec = ClusterSpec(name="t", source="/x", shards=4, replicas=2, keeper_nodes=3, port_base=1)
    span = port_span(spec)
    result = build_topology(spec)
    highest = max(
        [node.tcp_port for node in result.nodes] + [keeper.client_port for keeper in result.keepers]
    )
    assert 1 + span > highest


def test_topology_requires_allocated_ports():
    with pytest.raises(ValueError, match="port_base"):
        build_topology(ClusterSpec(name="t", source="/x"))


def test_node_lookup_reports_unknown_names():
    with pytest.raises(UnknownNode, match="clickhouse9"):
        topology().node("clickhouse9")


# --- allocation --------------------------------------------------------------------------


def test_allocate_block_skips_reserved_ranges():
    """Ports promised to a stopped instance are free at the OS level but must not be reused."""
    first = allocate_block(50)
    second = allocate_block(50, taken=set(range(first, first + 50)))
    assert second >= first + 50


def test_allocated_ports_are_actually_free():
    base = allocate_block(20)
    assert all(is_free(port) for port in range(base, base + 20))
