"""Rendering a topology into a docker-compose file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from . import services as companions
from .resolve import Resolved
from .topology import (
    CONTAINER_HTTP_PORT,
    CONTAINER_KEEPER_PORT,
    CONTAINER_TCP_PORT,
    KeeperNode,
    Node,
    Topology,
)

SERVER_CONFIG_DIR = "/etc/clickhouse-server"
KEEPER_CONFIG_PATH = "/etc/clickhouse-keeper/keeper_config.xml"
BINARY_PATH = "/usr/bin/clickhouse"
DATA_PATH = "/var/lib/clickhouse"
KEEPER_DATA_PATH = "/var/lib/clickhouse-keeper"

#: ClickHouse opens a lot of files; the Docker default of 1024 is not enough.
NOFILE_LIMIT = 262144


def render(topology: Topology, resolved: Resolved, root: Path, image: str) -> dict[str, Any]:
    """Build the compose document for an instance rooted at ``root``."""
    services: dict[str, Any] = {}
    volumes: dict[str, Any] = {}
    project = topology.spec.project_name

    for keeper in topology.keepers:
        services[keeper.name] = _keeper_service(keeper, topology, resolved, root, image, project)
        volumes[_volume_name(keeper.name)] = None

    for node in topology.nodes:
        services[node.name] = _node_service(node, topology, resolved, root, image, project)
        volumes[_volume_name(node.name)] = None

    for service, endpoint in companions.active(topology):
        services[service.name] = service.compose_service(topology, endpoint)
        for volume in service.volumes():
            volumes[volume] = None

    return {"services": services, "volumes": volumes}


def write(topology: Topology, resolved: Resolved, root: Path, image: str) -> Path:
    path = root / "docker-compose.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(render(topology, resolved, root, image), sort_keys=False))
    return path


def _node_service(
    node: Node, topology: Topology, resolved: Resolved, root: Path, image: str, project: str
) -> dict[str, Any]:
    config_dir = root / "configs" / node.name
    mounts = _binary_mount(resolved)

    if not resolved.runs_as_image:
        # Base-image mode: we own the whole configuration.
        mounts.append(_ro(config_dir / "config.xml", f"{SERVER_CONFIG_DIR}/config.xml"))
        mounts.append(_ro(config_dir / "users.xml", f"{SERVER_CONFIG_DIR}/users.xml"))

    # Overlays are mounted file by file rather than as directories: mounting the directory
    # would hide the image's own config.d, and stock images put their listen-address
    # configuration there.
    mounts += _overlay_mounts(config_dir / "config.d", f"{SERVER_CONFIG_DIR}/config.d")
    mounts += _overlay_mounts(config_dir / "users.d", f"{SERVER_CONFIG_DIR}/users.d")
    mounts.append(f"{_volume_name(node.name)}:{DATA_PATH}")

    service: dict[str, Any] = {
        "image": image,
        "container_name": f"{project}-{node.name}",
        "hostname": node.name,
        "volumes": mounts,
        "ports": [
            f"127.0.0.1:{node.http_port}:{CONTAINER_HTTP_PORT}",
            f"127.0.0.1:{node.tcp_port}:{CONTAINER_TCP_PORT}",
        ],
        "ulimits": {"nofile": {"soft": NOFILE_LIMIT, "hard": NOFILE_LIMIT}},
        # SYS_NICE only: IPC_LOCK would be needed for mlock, and the generated config sets
        # mlock_executable to false.
        "cap_add": ["SYS_NICE"],
        "restart": "unless-stopped",
    }

    if not resolved.runs_as_image:
        service["entrypoint"] = [
            BINARY_PATH,
            "server",
            f"--config-file={SERVER_CONFIG_DIR}/config.xml",
        ]

    if topology.keepers:
        service["depends_on"] = [keeper.name for keeper in topology.keepers]

    return service


def _keeper_service(
    keeper: KeeperNode, topology: Topology, resolved: Resolved, root: Path, image: str, project: str
) -> dict[str, Any]:
    config_path = root / "configs" / keeper.name / "keeper_config.xml"
    mounts = _binary_mount(resolved)
    mounts.append(_ro(config_path, KEEPER_CONFIG_PATH))
    mounts.append(f"{_volume_name(keeper.name)}:{KEEPER_DATA_PATH}")

    return {
        "image": image,
        "container_name": f"{project}-{keeper.name}",
        "hostname": keeper.name,
        "volumes": mounts,
        "ports": [f"127.0.0.1:{keeper.client_port}:{CONTAINER_KEEPER_PORT}"],
        # Keeper is our own construct, so its entrypoint is overridden in both modes.
        "entrypoint": [BINARY_PATH, "keeper", f"--config-file={KEEPER_CONFIG_PATH}"],
        "ulimits": {"nofile": {"soft": NOFILE_LIMIT, "hard": NOFILE_LIMIT}},
        "restart": "unless-stopped",
    }


def _binary_mount(resolved: Resolved) -> list[str]:
    """Docker sources run their own binary; everything else gets ours mounted in."""
    if resolved.runs_as_image or resolved.binary_path is None:
        return []
    return [_ro(resolved.binary_path, BINARY_PATH)]


def _overlay_mounts(source_dir: Path, target_dir: str) -> list[str]:
    if not source_dir.is_dir():
        return []
    return [_ro(path, f"{target_dir}/{path.name}") for path in sorted(source_dir.glob("*.xml"))]


def _ro(source: Path, target: str) -> str:
    return f"{source}:{target}:ro"


def _volume_name(service: str) -> str:
    return f"{service}-data"
