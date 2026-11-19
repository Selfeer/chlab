"""S3-compatible object storage, served by RustFS.

RustFS rather than MinIO because clickhouse-regression's Iceberg suite runs RustFS, so
behaviour seen here matches what CI sees. Everything is path-style: RustFS rejects
virtual-host requests, and ClickHouse uses path style for an endpoint like ``http://s3:9000``.

Buckets and extra identities are managed with ``mc``, run as a throwaway container on the
instance's network — no S3 client is needed on the host.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..errors import ChlabError
from ..events import EventCallback
from ..image import MC_IMAGE
from ..spec import S3_ROOT_PASSWORD, S3_ROOT_USER, ClusterSpec, S3UserSpec
from ..topology import ServiceNode, Topology
from .base import Service

IMAGE = "rustfs/rustfs:1.0.3"
HOSTNAME = "s3"
API_PORT = 9000
CONSOLE_PORT = 9001
#: Host port offsets within the service's reserved stride.
API_OFFSET = 0
CONSOLE_OFFSET = 1
#: The alias every mc command uses, so paths read ``s3/<bucket>/<key>``.
MC_ALIAS = "s3"
REGION = "us-east-1"
MC_TIMEOUT = 600
VOLUME = "s3-data"

#: Actions granted per access level, on the buckets a user is scoped to.
POLICY_ACTIONS = {
    "readonly": ["s3:GetObject", "s3:ListBucket", "s3:GetBucketLocation"],
    "writeonly": [
        "s3:PutObject",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
        "s3:ListBucketMultipartUploads",
        "s3:GetBucketLocation",
    ],
    "readwrite": ["s3:*"],
}


class S3Error(ChlabError):
    pass


class S3(Service):
    name = HOSTNAME

    # --- compose -------------------------------------------------------------------------

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        return {
            "image": IMAGE,
            "container_name": f"{topology.spec.project_name}-{self.name}",
            "hostname": HOSTNAME,
            "command": ["/data"],
            "environment": {
                "RUSTFS_ACCESS_KEY": S3_ROOT_USER,
                "RUSTFS_SECRET_KEY": S3_ROOT_PASSWORD,
                "RUSTFS_ADDRESS": f":{API_PORT}",
                "RUSTFS_CONSOLE_ENABLE": "true",
                "RUSTFS_OBS_USE_STDOUT": "true",
                "RUSTFS_OBS_LOG_DIRECTORY": "",
                # A lab trades durability for speed, as clickhouse-regression does.
                "RUSTFS_DURABILITY_MODE": "relaxed",
            },
            "volumes": [f"{VOLUME}:/data"],
            "ports": [
                f"127.0.0.1:{node.port(API_OFFSET)}:{API_PORT}",
                f"127.0.0.1:{node.port(CONSOLE_OFFSET)}:{CONSOLE_PORT}",
            ],
            "restart": "unless-stopped",
        }

    def volumes(self) -> list[str]:
        return [VOLUME]

    # --- ClickHouse side -----------------------------------------------------------------

    def node_configs(self, topology: Topology) -> dict[str, dict[str, Any]]:
        return {"60-chlab-s3.xml": {"named_collections": named_collections(topology.spec)}}

    # --- lifecycle -----------------------------------------------------------------------

    def is_ready(self, topology: Topology, node: ServiceNode) -> bool:
        try:
            url = f"http://127.0.0.1:{node.port(API_OFFSET)}/health"
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def initialise(self, topology: Topology, node: ServiceNode, notify: EventCallback) -> None:
        spec = topology.spec
        notify(f"creating bucket(s) {', '.join(spec.effective_buckets)}")
        run_mc(spec, ["mb", "--ignore-existing", *bucket_paths(spec.effective_buckets)])
        for user in spec.s3_users:
            notify(f"creating S3 user {user.name}")
            _create_user(spec, user)

    def tool_env(self, topology: Topology, node: ServiceNode) -> dict[str, str]:
        spec = topology.spec
        return {
            # boto3 1.28+ and the AWS CLI honour AWS_ENDPOINT_URL.
            "AWS_ENDPOINT_URL": network_endpoint(),
            "AWS_ACCESS_KEY_ID": S3_ROOT_USER,
            "AWS_SECRET_ACCESS_KEY": S3_ROOT_PASSWORD,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "CHLAB_S3_ENDPOINT": network_endpoint(),
            "CHLAB_S3_BUCKET": spec.effective_buckets[0],
            "CHLAB_S3_BUCKETS": ",".join(spec.effective_buckets),
            f"MC_HOST_{MC_ALIAS}": f"http://{S3_ROOT_USER}:{S3_ROOT_PASSWORD}@{HOSTNAME}:{API_PORT}",
        }

    def describe(self, topology: Topology, node: ServiceNode) -> dict[str, Any]:
        spec = topology.spec
        default_bucket = spec.effective_buckets[0]
        return {
            "image": IMAGE,
            "container": f"{spec.project_name}-{self.name}",
            "host": {
                "endpoint": f"http://127.0.0.1:{node.port(API_OFFSET)}",
                "console": f"http://127.0.0.1:{node.port(CONSOLE_OFFSET)}",
            },
            "network": {"endpoint": network_endpoint()},
            "credentials": {
                "access_key_id": S3_ROOT_USER,
                "secret_access_key": S3_ROOT_PASSWORD,
            },
            "buckets": list(spec.effective_buckets),
            "users": [
                {
                    "name": user.name,
                    "secret": user.secret,
                    "access": user.access,
                    "buckets": list(user.buckets) or list(spec.effective_buckets),
                }
                for user in spec.s3_users
            ],
            "named_collections": list(named_collections(spec)),
            "sql": {
                "read": "SELECT * FROM s3(s3, filename = 'path/*.parquet')",
                "write": (
                    "INSERT INTO FUNCTION s3(s3, filename = 'path/data.parquet') "
                    "SELECT number FROM numbers(10)"
                ),
                "explicit": (
                    f"SELECT * FROM s3('{network_endpoint()}/{default_bucket}/path/*.parquet', "
                    f"'{S3_ROOT_USER}', '{S3_ROOT_PASSWORD}')"
                ),
                "list": "SELECT _path, _size FROM s3(s3, filename = '**', format = 'One')",
                **iceberg_without_catalog_sql(spec),
            },
        }


# --- named collections ---------------------------------------------------------------------


def named_collections(spec: ClusterSpec) -> dict[str, dict[str, str]]:
    """``s3`` for the first bucket, ``s3_<bucket>`` for each.

    The collections carry the root credentials, so ``s3(s3, filename = 'x.parquet')`` works
    without pasting keys into every query.
    """
    collections = {}
    for index, bucket in enumerate(spec.effective_buckets):
        collection = _collection(bucket)
        if index == 0:
            collections["s3"] = collection
        collections["s3_" + bucket.replace("-", "_").replace(".", "_")] = collection
    return collections


def _collection(bucket: str) -> dict[str, str]:
    return {
        "url": f"{network_endpoint()}/{bucket}/",
        "access_key_id": S3_ROOT_USER,
        "secret_access_key": S3_ROOT_PASSWORD,
    }


def network_endpoint() -> str:
    return f"http://{HOSTNAME}:{API_PORT}"


# --- mc ------------------------------------------------------------------------------------


def mc_command(
    spec: ClusterSpec,
    args: Iterable[str],
    *,
    as_user: str | None = None,
    mounts: Iterable[tuple[str, str, bool]] = (),
    interactive: bool = False,
) -> list[str]:
    """``docker run`` invocation of ``mc`` on the instance's network.

    ``mounts`` are ``(host_path, container_path, read_only)``. The container runs as the
    invoking user, so files it writes to a mounted host directory are not owned by root.
    """
    key, secret = credentials(spec, as_user)
    host = "http://{}:{}@{}:{}".format(
        urllib.parse.quote(key, safe=""), urllib.parse.quote(secret, safe=""), HOSTNAME, API_PORT
    )
    command = ["docker", "run", "--rm"]
    if interactive:
        command.append("-i")
    command += [
        "--network",
        spec.network_name,
        "-e",
        f"MC_HOST_{MC_ALIAS}={host}",
        "-e",
        "MC_CONFIG_DIR=/tmp/.mc",
    ]
    if hasattr(os, "getuid"):
        command += ["--user", f"{os.getuid()}:{os.getgid()}"]
    for host_path, container_path, read_only in mounts:
        command += ["-v", f"{host_path}:{container_path}" + (":ro" if read_only else "")]
    return [*command, MC_IMAGE, *args]


def credentials(spec: ClusterSpec, as_user: str | None) -> tuple[str, str]:
    if as_user is None or as_user == S3_ROOT_USER:
        return S3_ROOT_USER, S3_ROOT_PASSWORD
    for user in spec.s3_users:
        if user.name == as_user:
            return user.name, user.secret
    known = ", ".join([S3_ROOT_USER, *(user.name for user in spec.s3_users)])
    raise S3Error(f"{spec.name} has no S3 user {as_user!r}; expected one of {known}")


def run_mc(spec: ClusterSpec, args: list[str], *, stdin: str | None = None) -> str:
    """Run mc and return its output, raising with mc's own message on failure."""
    result = subprocess.run(
        mc_command(spec, args, interactive=stdin is not None),
        input=stdin,
        capture_output=True,
        text=True,
        timeout=MC_TIMEOUT,
    )
    if result.returncode != 0:
        raise S3Error(
            f"mc {' '.join(args[:2])} failed: " + (result.stderr.strip() or result.stdout.strip())
        )
    return result.stdout


def bucket_paths(buckets: Iterable[str]) -> list[str]:
    return [f"{MC_ALIAS}/{bucket}" for bucket in buckets]


def object_path(path: str) -> str:
    """``warehouse/a/b`` -> ``s3/warehouse/a/b``; an explicit ``s3/`` prefix is kept."""
    path = path.lstrip("/")
    if path == MC_ALIAS or path.startswith(MC_ALIAS + "/"):
        return path
    return f"{MC_ALIAS}/{path}"


def policy_document(spec: ClusterSpec, user: S3UserSpec) -> dict[str, Any]:
    buckets = user.buckets or spec.effective_buckets
    resources = [
        arn for bucket in buckets for arn in (f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*")
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": POLICY_ACTIONS[user.access], "Resource": resources}
        ],
    }


def _create_user(spec: ClusterSpec, user: S3UserSpec) -> None:
    policy = f"chlab-{user.name}"
    run_mc(spec, ["admin", "user", "add", MC_ALIAS, user.name, user.secret])
    run_mc(
        spec,
        ["admin", "policy", "create", MC_ALIAS, policy, "/dev/stdin"],
        stdin=json.dumps(policy_document(spec, user)),
    )
    try:
        run_mc(spec, ["admin", "policy", "attach", MC_ALIAS, policy, "--user", user.name])
    except S3Error as failure:
        # Re-running setup on a recreated instance finds the policy already attached.
        if "already" not in str(failure).lower():
            raise


def iceberg_without_catalog_sql(spec: ClusterSpec) -> dict[str, str]:
    """Catalog-less Iceberg over the s3 service: a table read straight from its path.

    ``icebergS3`` exists from 24.9; before that the same function is called ``iceberg``.
    """
    function = "iceberg" if version_below(spec.version, (24, 9)) else "icebergS3"
    return {
        "iceberg_read": f"SELECT * FROM {function}(s3, filename = 'path/to/table')",
        "iceberg_table": (
            "CREATE TABLE t ENGINE = IcebergS3(s3, filename = 'path/to/table') "
            "SETTINGS allow_dynamic_metadata_for_data_lakes = 1"
        ),
        "iceberg_local": (
            "CREATE TABLE t (a Int32) ENGINE = IcebergLocal("
            "'/var/lib/clickhouse/user_files/t/', 'Parquet')"
        ),
    }


def version_below(version: str | None, bound: tuple[int, ...]) -> bool:
    """True when a known version is older than ``bound``. Unknown versions count as new."""
    if not version:
        return False
    parts = []
    for piece in version.split(".")[: len(bound)]:
        digits = "".join(character for character in piece if character.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    if not parts:
        return False  # "head", "latest": not a version, and not an old one either
    return tuple(parts) < bound
