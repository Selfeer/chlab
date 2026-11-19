"""Everything needed to talk to an instance, in one document.

``chlab env`` prints this. It answers the questions anyone driving an instance asks first —
which port, which container, which credentials, what is it called *inside* the network — so
none of that has to be reverse-engineered from the compose file.
"""

from __future__ import annotations

from typing import Any

from . import services as companions
from .registry import Registry
from .runtime import InstanceStatus
from .topology import (
    CONTAINER_HTTP_PORT,
    CONTAINER_KEEPER_PORT,
    CONTAINER_TCP_PORT,
)


def describe(status: InstanceStatus, registry: Registry) -> dict[str, Any]:
    spec = status.spec
    topology = status.topology
    return {
        "name": spec.name,
        "state": status.state,
        "version": spec.version,
        "source": spec.source,
        "cluster": spec.cluster_name,
        "compose_project": spec.project_name,
        # Anything attached to this network resolves the services below by their hostnames.
        "network": spec.network_name,
        "directory": str(registry.instance_dir(spec.name)),
        "nodes": [
            {
                "name": node.name,
                "container": topology.container(node.name),
                "shard": node.shard,
                "replica": node.replica,
                "host": {
                    "http": node.http_url,
                    "native": f"127.0.0.1:{node.tcp_port}",
                    "play": node.play_url,
                },
                "network": {
                    "http": f"http://{node.name}:{CONTAINER_HTTP_PORT}",
                    "native": f"{node.name}:{CONTAINER_TCP_PORT}",
                },
            }
            for node in topology.nodes
        ],
        "keepers": [
            {
                "name": keeper.name,
                "container": topology.container(keeper.name),
                "host": f"127.0.0.1:{keeper.client_port}",
                "network": f"{keeper.name}:{CONTAINER_KEEPER_PORT}",
            }
            for keeper in topology.keepers
        ],
        "users": [
            {"name": "default", "password": "", "role": "admin"},
            *(
                {"name": user.name, "password": user.password, "role": user.role}
                for user in spec.users
            ),
        ],
        "databases": [
            {"name": database.name, "engine": database.engine} for database in spec.databases
        ],
        "services": {
            service.name: service.describe(topology, endpoint)
            for service, endpoint in companions.active(topology)
        },
        "settings": {"server": dict(spec.settings), "profile": dict(spec.profile_settings)},
        "seed": spec.seed,
    }
