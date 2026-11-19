"""Generation of ClickHouse server, user and Keeper configuration.

Two modes, matching the two ways chlab runs a build:

*Base-image mode* (a resolved binary) writes a complete ``config.xml`` and ``users.xml``,
because ``clickhouse-common-static`` ships only the binary — the stock configs live in the
``clickhouse-server`` package, which we deliberately do not install.

*Docker-image mode* writes only ``config.d`` / ``users.d`` overlays and leaves the image's own
configuration alone, so what runs is that image as its authors shipped it.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from . import services as companions
from .spec import ClusterSpec
from .topology import (
    CONTAINER_HTTP_PORT,
    CONTAINER_INTERSERVER_PORT,
    CONTAINER_KEEPER_PORT,
    CONTAINER_KEEPER_RAFT_PORT,
    CONTAINER_TCP_PORT,
    KeeperNode,
    Node,
    Topology,
)

ROOT_TAG = "clickhouse"
CONFIG_D = "config.d"
USERS_D = "users.d"


def write_configs(topology: Topology, root: Path, *, base_image_mode: bool) -> None:
    """Write every configuration file for an instance under ``root/configs``."""
    configs = root / "configs"
    for node in topology.nodes:
        _write_node_configs(topology, node, configs / node.name, base_image_mode=base_image_mode)
    for keeper in topology.keepers:
        _write_keeper_configs(topology, keeper, configs / keeper.name)


def _write_node_configs(
    topology: Topology, node: Node, node_dir: Path, *, base_image_mode: bool
) -> None:
    config_d = node_dir / CONFIG_D
    users_d = node_dir / USERS_D
    config_d.mkdir(parents=True, exist_ok=True)
    users_d.mkdir(parents=True, exist_ok=True)

    if base_image_mode:
        _write_xml(node_dir / "config.xml", base_config())
        _write_xml(node_dir / "users.xml", base_users())

    _write_xml(config_d / "10-chlab-cluster.xml", remote_servers(topology))
    _write_xml(config_d / "20-chlab-macros.xml", macros(topology.spec, node))
    _write_xml(config_d / "40-chlab-logging.xml", console_logging())

    if topology.uses_keeper:
        _write_xml(config_d / "30-chlab-keeper.xml", zookeeper(topology.keepers))

    if topology.spec.settings:
        _write_xml(config_d / "50-chlab-settings.xml", server_settings(topology.spec.settings))

    for service, _endpoint in companions.active(topology):
        for filename, document in service.node_configs(topology).items():
            _write_xml(config_d / filename, document)

    # The declared users are created as SQL objects after start; this only makes sure the
    # default user is allowed to create them.
    _write_xml(users_d / "10-chlab-default.xml", default_user_access_management())

    if topology.spec.profile_settings:
        _write_xml(
            users_d / "20-chlab-profile.xml", profile_settings(topology.spec.profile_settings)
        )


def _write_keeper_configs(topology: Topology, keeper: KeeperNode, keeper_dir: Path) -> None:
    keeper_dir.mkdir(parents=True, exist_ok=True)
    _write_xml(keeper_dir / "keeper_config.xml", keeper_config(keeper, topology.keepers))


# --- document builders -------------------------------------------------------------------


def base_config() -> dict[str, Any]:
    """A complete, minimal ``config.xml`` for running a bare static binary."""
    return {
        "logger": {
            "level": "information",
            "log": "/var/log/clickhouse-server/clickhouse-server.log",
            "errorlog": "/var/log/clickhouse-server/clickhouse-server.err.log",
            "size": "100M",
            "count": "3",
        },
        "http_port": CONTAINER_HTTP_PORT,
        "tcp_port": CONTAINER_TCP_PORT,
        "interserver_http_port": CONTAINER_INTERSERVER_PORT,
        "listen_host": "0.0.0.0",
        "path": "/var/lib/clickhouse/",
        "tmp_path": "/var/lib/clickhouse/tmp/",
        "user_files_path": "/var/lib/clickhouse/user_files/",
        "format_schema_path": "/var/lib/clickhouse/format_schemas/",
        "user_directories": {
            "users_xml": {"path": "users.xml"},
            # SQL-created users and grants persist here.
            "local_directory": {"path": "/var/lib/clickhouse/access/"},
        },
        "default_profile": "default",
        "default_database": "default",
        "timezone": "UTC",
        "mlock_executable": "false",
        # Lab-scale, not server-scale. The stock defaults (5 GiB / 8 GiB) assume one server
        # owns the machine; chlab's whole point is several clusters of several nodes side by
        # side on a laptop. --setting overrides any of this.
        "mark_cache_size": 268435456,
        "uncompressed_cache_size": 536870912,
        "max_server_memory_usage_to_ram_ratio": 0.3,
        "max_connections": 1024,
        "max_concurrent_queries": 100,
        "keep_alive_timeout": 3,
        # Stock images ship this; a bare binary has no query log unless asked. Reproductions
        # read ProfileEvents from it (S3 GETs per query, bytes read), so it must exist.
        "query_log": {
            "database": "system",
            "table": "query_log",
            "flush_interval_milliseconds": 7500,
        },
    }


def base_users() -> dict[str, Any]:
    """A ``users.xml`` with a single passwordless ``default`` user, as ClickHouse ships."""
    return {
        "profiles": {"default": {}},
        "quotas": {"default": {}},
        "users": {
            "default": {
                "password": "",
                "networks": {"ip": "::/0"},
                "profile": "default",
                "quota": "default",
                "access_management": 1,
                "named_collection_control": 1,
            }
        },
    }


def default_user_access_management() -> dict[str, Any]:
    """Let ``default`` issue CREATE USER / GRANT, which stock docker images disallow."""
    return {
        "users": {
            "default": {
                "access_management": 1,
                "named_collection_control": 1,
            }
        }
    }


def remote_servers(topology: Topology) -> dict[str, Any]:
    shards = []
    for shard_nodes in topology.shard_layout():
        shards.append(
            {
                "internal_replication": "true",
                "replica": [
                    {"host": node.name, "port": CONTAINER_TCP_PORT} for node in shard_nodes
                ],
            }
        )
    return {"remote_servers": {topology.spec.cluster_name: {"shard": shards}}}


def macros(spec: ClusterSpec, node: Node) -> dict[str, Any]:
    return {
        "macros": {
            "shard": str(node.shard),
            "replica": node.name,
            "cluster": spec.cluster_name,
        }
    }


def zookeeper(keepers: tuple[KeeperNode, ...]) -> dict[str, Any]:
    return {
        "zookeeper": {
            "node": [{"host": keeper.name, "port": CONTAINER_KEEPER_PORT} for keeper in keepers]
        },
        "distributed_ddl": {"path": "/clickhouse/task_queue/ddl"},
    }


def console_logging() -> dict[str, Any]:
    """Mirror the server log to stdout so ``chlab logs`` works through Docker."""
    return {"logger": {"console": 1}}


def server_settings(settings: dict[str, str]) -> dict[str, Any]:
    return dict(settings)


def profile_settings(settings: dict[str, str]) -> dict[str, Any]:
    """Query settings for every session, via the ``default`` profile.

    SQL-created users inherit ``default_profile`` too, so this reaches them without a
    per-user ``SETTINGS PROFILE`` clause.
    """
    return {"profiles": {"default": dict(settings)}}


def keeper_config(keeper: KeeperNode, keepers: tuple[KeeperNode, ...]) -> dict[str, Any]:
    return {
        "logger": {"level": "information", "console": 1},
        "listen_host": "0.0.0.0",
        "keeper_server": {
            "tcp_port": CONTAINER_KEEPER_PORT,
            "server_id": keeper.index,
            "log_storage_path": "/var/lib/clickhouse-keeper/coordination/log",
            "snapshot_storage_path": "/var/lib/clickhouse-keeper/coordination/snapshots",
            "coordination_settings": {
                "operation_timeout_ms": 10000,
                "session_timeout_ms": 30000,
                "raft_logs_level": "warning",
            },
            "raft_configuration": {
                "server": [
                    {
                        "id": other.index,
                        "hostname": other.name,
                        "port": CONTAINER_KEEPER_RAFT_PORT,
                    }
                    for other in keepers
                ]
            },
        },
    }


# --- XML rendering -----------------------------------------------------------------------


def to_xml(document: dict[str, Any], root_tag: str = ROOT_TAG) -> str:
    root = ET.Element(root_tag)
    _build(root, document)
    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="unicode") + "\n"


def _build(parent: ET.Element, value: Any) -> None:
    """Render a nested dict into elements.

    A list value repeats its tag — that is how ClickHouse expresses several ``<replica>`` or
    ``<node>`` entries under one parent.
    """
    if not isinstance(value, dict):
        parent.text = _text(value)
        return

    for key, child_value in value.items():
        if isinstance(child_value, list):
            for item in child_value:
                child = ET.SubElement(parent, key)
                _build(child, item)
        else:
            child = ET.SubElement(parent, key)
            _build(child, child_value)


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_xml(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(to_xml(document))
