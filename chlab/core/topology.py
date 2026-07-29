"""Turning a ClusterSpec into concrete nodes, ports and cluster layout."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ChlabError
from .spec import MAX_NODES, ClusterSpec

#: Host ports for a node are ``port_base + index * NODE_PORT_STRIDE + offset``.
NODE_PORT_STRIDE = 10
HTTP_OFFSET = 0
TCP_OFFSET = 1
#: Keeper client ports sit immediately above the largest possible ClickHouse block. Derived
#: rather than chosen, so the two ranges cannot silently overlap if MAX_NODES ever changes.
KEEPER_PORT_BASE_OFFSET = MAX_NODES * NODE_PORT_STRIDE

CONTAINER_HTTP_PORT = 8123
CONTAINER_TCP_PORT = 9000
CONTAINER_INTERSERVER_PORT = 9009
CONTAINER_KEEPER_PORT = 9181
CONTAINER_KEEPER_RAFT_PORT = 9234


class UnknownNode(ChlabError):
    pass


@dataclass(frozen=True)
class Node:
    """One ClickHouse server in the cluster."""

    name: str
    #: 1-based position in the cluster.
    index: int
    shard: int
    replica: int
    http_port: int
    tcp_port: int

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.http_port}"

    @property
    def play_url(self) -> str:
        """ClickHouse's own query UI, served by the node itself."""
        return f"{self.http_url}/play"


@dataclass(frozen=True)
class KeeperNode:
    name: str
    #: 1-based; also used as the Raft ``server_id``.
    index: int
    client_port: int


@dataclass(frozen=True)
class Topology:
    spec: ClusterSpec
    nodes: tuple[Node, ...]
    keepers: tuple[KeeperNode, ...]

    @property
    def uses_keeper(self) -> bool:
        return bool(self.keepers)

    def node(self, name: str) -> Node:
        for node in self.nodes:
            if node.name == name:
                return node
        raise UnknownNode(
            f"{name!r} is not a node of {self.spec.name}; "
            f"expected one of {', '.join(self.node_names)}"
        )

    @property
    def node_names(self) -> tuple[str, ...]:
        return tuple(node.name for node in self.nodes)

    def shard_layout(self) -> list[list[Node]]:
        """Nodes grouped by shard, in shard order."""
        return [
            [node for node in self.nodes if node.shard == shard]
            for shard in range(1, self.spec.shards + 1)
        ]


def build_topology(spec: ClusterSpec) -> Topology:
    """Assign nodes to shards and replicas, and hand out host ports.

    Nodes fill shard by shard: with 2 shards and 2 replicas, ``clickhouse1`` and
    ``clickhouse2`` are the replicas of shard 1, ``clickhouse3`` and ``clickhouse4`` of shard 2.
    """
    if spec.port_base is None:
        raise ValueError("spec has no allocated port_base; call allocate_ports first")

    nodes = []
    for index in range(1, spec.node_count + 1):
        shard = (index - 1) // spec.replicas + 1
        replica = (index - 1) % spec.replicas + 1
        offset = spec.port_base + (index - 1) * NODE_PORT_STRIDE
        nodes.append(
            Node(
                name=f"clickhouse{index}",
                index=index,
                shard=shard,
                replica=replica,
                http_port=offset + HTTP_OFFSET,
                tcp_port=offset + TCP_OFFSET,
            )
        )

    keepers = tuple(
        KeeperNode(
            name=f"keeper{index}",
            index=index,
            client_port=spec.port_base + KEEPER_PORT_BASE_OFFSET + (index - 1) * NODE_PORT_STRIDE,
        )
        for index in range(1, spec.effective_keeper_nodes + 1)
    )

    return Topology(spec=spec, nodes=tuple(nodes), keepers=keepers)


def port_span(spec: ClusterSpec) -> int:
    """How many consecutive host ports an instance needs, from ``port_base``."""
    keeper_span = KEEPER_PORT_BASE_OFFSET + max(spec.effective_keeper_nodes, 1) * NODE_PORT_STRIDE
    node_span = spec.node_count * NODE_PORT_STRIDE
    return max(node_span, keeper_span)
