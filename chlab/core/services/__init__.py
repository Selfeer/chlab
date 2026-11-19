"""Companion services, by the name ``--with`` uses."""

from __future__ import annotations

from collections.abc import Iterator

from ..spec import KNOWN_SERVICES
from ..topology import ServiceNode, Topology
from .base import Service
from .catalogs import IcebergRestFixture, IceRestCatalog
from .s3 import S3
from .spark import Spark
from .toxiproxy import Toxiproxy

REGISTRY: dict[str, Service] = {
    service.name: service
    for service in (S3(), IcebergRestFixture(), IceRestCatalog(), Spark(), Toxiproxy())
}

if set(REGISTRY) != set(KNOWN_SERVICES):
    raise RuntimeError("spec.KNOWN_SERVICES and services.REGISTRY disagree")


def active(topology: Topology) -> Iterator[tuple[Service, ServiceNode]]:
    """The services an instance runs, each with its allocated ports."""
    for node in topology.services:
        yield REGISTRY[node.name], node
