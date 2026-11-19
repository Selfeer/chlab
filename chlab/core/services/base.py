"""What a companion service contributes to an instance.

A service is everything chlab needs to know to run one extra container next to the cluster:
its compose definition, the ClickHouse configuration that points at it, how to tell it is
ready, what to set up inside it once it is, and how to describe it in ``chlab env``. Adding a
service means writing one of these; nothing else in chlab branches on service names.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..cache import Cache
from ..events import EventCallback
from ..topology import ServiceNode, Topology


class Service:
    #: The name used with ``--with``, also the compose service name and network hostname.
    name: str

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        """The compose definition. ``root`` is the instance directory, where ``files`` live."""
        raise NotImplementedError

    def prepare(self, topology: Topology, root: Path, cache: Cache, notify: EventCallback) -> None:
        """Before launch: fetch what the service needs into ``root/services/<name>/``."""

    def files(self, topology: Topology) -> dict[str, str]:
        """Files to write under ``<instance>/services/<name>/``, keyed by relative path."""
        return {}

    def tool_env(self, topology: Topology, node: ServiceNode) -> dict[str, str]:
        """Environment for the tools container, so scripts find this service unconfigured."""
        return {}

    def volumes(self) -> list[str]:
        """Named volumes the compose service uses. Removed by ``chlab rm``."""
        return []

    def node_configs(self, topology: Topology) -> dict[str, dict[str, Any]]:
        """``config.d`` documents for every ClickHouse node, keyed by file name."""
        return {}

    def is_ready(self, topology: Topology, node: ServiceNode) -> bool:
        return True

    def initialise(self, topology: Topology, node: ServiceNode, notify: EventCallback) -> None:
        """One-time setup after the service is ready: buckets, users, catalogs."""

    def describe(self, topology: Topology, node: ServiceNode) -> dict[str, Any]:
        return {}
