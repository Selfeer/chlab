"""A fault-injecting proxy in front of the s3 service.

Object-storage bugs often only show under conditions a local S3 never has: latency per
request (where a change in request count turns into minutes), a slow link, a server that
stops answering. Toxiproxy sits between ClickHouse and RustFS and adds those on demand.

ClickHouse reaches storage through the proxy via named collections ``s3_proxy`` (first
bucket) and ``s3_proxy_<bucket>``; ``s3`` stays direct, so the same data can be read both ways
and compared. ``chlab proxy`` adds and removes faults while the cluster runs.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..errors import ChlabError
from ..spec import S3_ROOT_PASSWORD, S3_ROOT_USER, ClusterSpec
from ..topology import ServiceNode, Topology
from . import s3
from .base import Service

IMAGE = "ghcr.io/shopify/toxiproxy:2.12.0"
HOSTNAME = "toxiproxy"
API_PORT = 8474
PROXY_PORT = 9100
#: The one proxy: the s3 service's API.
PROXY = "s3"
API_OFFSET = 0
PROXY_OFFSET = 1

#: Faults ``chlab proxy`` can apply, with the toxiproxy attribute each value sets.
TOXICS = {
    "latency": "latency",  # milliseconds added to every chunk of data
    "bandwidth": "rate",  # KB/s
    "timeout": "timeout",  # stop all data, close after N ms (0: hold forever)
    "slow_close": "delay",  # ms before the connection closes
}


class ProxyError(ChlabError):
    pass


class Toxiproxy(Service):
    name = HOSTNAME

    def files(self, topology: Topology) -> dict[str, str]:
        proxies = [
            {
                "name": PROXY,
                "listen": f"0.0.0.0:{PROXY_PORT}",
                "upstream": f"{s3.HOSTNAME}:{s3.API_PORT}",
                "enabled": True,
            }
        ]
        return {"toxiproxy.json": json.dumps(proxies, indent=2) + "\n"}

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        spec = topology.spec
        config = root / "services" / self.name / "toxiproxy.json"
        return {
            "image": IMAGE,
            "container_name": f"{spec.project_name}-{self.name}",
            "hostname": HOSTNAME,
            "command": ["-host=0.0.0.0", "-config=/config/toxiproxy.json"],
            "volumes": [f"{config}:/config/toxiproxy.json:ro"],
            "ports": [
                f"127.0.0.1:{node.port(API_OFFSET)}:{API_PORT}",
                f"127.0.0.1:{node.port(PROXY_OFFSET)}:{PROXY_PORT}",
            ],
            "depends_on": [s3.HOSTNAME],
            "restart": "unless-stopped",
        }

    def node_configs(self, topology: Topology) -> dict[str, dict[str, Any]]:
        return {"61-chlab-toxiproxy.xml": {"named_collections": named_collections(topology.spec)}}

    def is_ready(self, topology: Topology, node: ServiceNode) -> bool:
        try:
            with urllib.request.urlopen(api_url(node, "/version"), timeout=3) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def tool_env(self, topology: Topology, node: ServiceNode) -> dict[str, str]:
        return {
            "CHLAB_S3_PROXY_ENDPOINT": network_endpoint(),
            "CHLAB_TOXIPROXY_API": f"http://{HOSTNAME}:{API_PORT}",
        }

    def describe(self, topology: Topology, node: ServiceNode) -> dict[str, Any]:
        spec = topology.spec
        return {
            "image": IMAGE,
            "container": f"{spec.project_name}-{self.name}",
            "host": {
                "endpoint": f"http://127.0.0.1:{node.port(PROXY_OFFSET)}",
                "api": api_url(node, ""),
            },
            "network": {"endpoint": network_endpoint()},
            "upstream": s3.network_endpoint(),
            "named_collections": list(named_collections(spec)),
            "sql": {
                "read": "SELECT count() FROM s3(s3_proxy, filename = 'path/*.parquet')",
                "faults": f"chlab proxy latency {spec.name} 500   # then: chlab proxy reset {spec.name}",
            },
        }


def named_collections(spec: ClusterSpec) -> dict[str, dict[str, str]]:
    collections = {}
    for index, bucket in enumerate(spec.effective_buckets):
        collection = {
            "url": f"{network_endpoint()}/{bucket}/",
            "access_key_id": S3_ROOT_USER,
            "secret_access_key": S3_ROOT_PASSWORD,
        }
        if index == 0:
            collections["s3_proxy"] = collection
        collections["s3_proxy_" + bucket.replace("-", "_").replace(".", "_")] = collection
    return collections


def network_endpoint() -> str:
    return f"http://{HOSTNAME}:{PROXY_PORT}"


def api_url(node: ServiceNode, path: str) -> str:
    return f"http://127.0.0.1:{node.port(API_OFFSET)}{path}"


# --- the toxiproxy API ---------------------------------------------------------------------


def _request(node: ServiceNode, method: str, path: str, body: Any = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        api_url(node, path), data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read().decode()
    except urllib.error.HTTPError as error:
        raise ProxyError(
            f"toxiproxy refused {method} {path}: {error.read().decode().strip()}"
        ) from error
    except (urllib.error.URLError, OSError) as error:
        raise ProxyError(
            f"toxiproxy is not reachable at {api_url(node, '')}; is the instance running?"
        ) from error
    return json.loads(payload) if payload.strip() else None


def status(node: ServiceNode) -> dict[str, Any]:
    proxy = _request(node, "GET", f"/proxies/{PROXY}")
    return {
        "enabled": proxy["enabled"],
        "listen": proxy["listen"],
        "upstream": proxy["upstream"],
        "toxics": [
            {
                "name": toxic["name"],
                "type": toxic["type"],
                "stream": toxic["stream"],
                **toxic["attributes"],
            }
            for toxic in proxy.get("toxics") or []
        ],
    }


def set_toxic(
    node: ServiceNode, kind: str, value: int, *, jitter: int = 0, upstream: bool = False
) -> None:
    """Apply a fault, replacing any earlier one of the same kind and direction."""
    if kind not in TOXICS:
        raise ProxyError(f"unknown fault {kind!r}; expected one of {', '.join(TOXICS)}")
    stream = "upstream" if upstream else "downstream"
    name = f"{kind}_{stream}"
    attributes: dict[str, int] = {TOXICS[kind]: value}
    if kind == "latency":
        attributes["jitter"] = jitter
    existing = {toxic["name"] for toxic in status(node)["toxics"]}
    if name in existing:
        _request(node, "DELETE", f"/proxies/{PROXY}/toxics/{name}")
    _request(
        node,
        "POST",
        f"/proxies/{PROXY}/toxics",
        {"name": name, "type": kind, "stream": stream, "toxicity": 1.0, "attributes": attributes},
    )


def set_enabled(node: ServiceNode, enabled: bool) -> None:
    """A disabled proxy refuses connections: storage that is simply gone."""
    _request(node, "POST", f"/proxies/{PROXY}", {"enabled": enabled})


def reset(node: ServiceNode) -> None:
    """Remove every fault and re-enable the proxy."""
    _request(node, "POST", "/reset")
