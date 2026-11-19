"""Companion services, by the name ``--with`` uses."""

from __future__ import annotations

from collections.abc import Iterator

from ..spec import KNOWN_SERVICES
from ..topology import ServiceNode, Topology
from .base import Service
from .s3 import S3

REGISTRY: dict[str, Service] = {service.name: service for service in (S3(),)}

assert set(REGISTRY) == set(KNOWN_SERVICES), "spec.KNOWN_SERVICES and REGISTRY disagree"


def active(topology: Topology) -> Iterator[tuple[Service, ServiceNode]]:
    """The services an instance runs, each with its allocated ports."""
    for node in topology.services:
        yield REGISTRY[node.name], node
