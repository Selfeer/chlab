"""Iceberg REST catalogs, both backed by the s3 service.

Two catalogs because they behave differently and findings depend on which one is in front of
ClickHouse:

* ``iceberg-rest`` is Apache's reference fixture (``apache/iceberg-rest-fixture``): the plain
  REST protocol, no auth, supports format v3 / deletion vectors from 1.9.
* ``ice-rest`` is Altinity's catalog (``altinity/ice-rest-catalog``), with bearer-token auth.

Both are what clickhouse-regression's Iceberg suite runs, with its versions. Each keeps its
catalog database on a named volume, so tables survive ``stop``/``start`` like the data does.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..spec import S3_ROOT_PASSWORD, S3_ROOT_USER, ClusterSpec
from ..topology import ServiceNode, Topology
from . import s3
from .base import Service

#: Query setting a catalog database needs to be created.
DATABASE_SETTING = "allow_experimental_database_iceberg"
#: Host port offset of each catalog's HTTP API within its reserved stride.
API_OFFSET = 0


class RestCatalog(Service):
    """What the two REST catalogs have in common."""

    #: Port the catalog listens on inside the network.
    port: int
    #: Name scripts pass to ``pyiceberg.catalog.load_catalog``.
    pyiceberg_name: str
    #: What ClickHouse appends to the base URL. The fixture is addressed as ``.../v1`` by
    #: DataLakeCatalog, ice-rest by its root; pyiceberg always takes the root and adds
    #: ``/v1`` itself.
    clickhouse_path: str = ""
    #: Bearer token the catalog requires, if any.
    token: str | None = None

    # --- what differs per catalog --------------------------------------------------------

    def warehouse_setting(self, spec: ClusterSpec) -> str:
        raise NotImplementedError

    # --- shared --------------------------------------------------------------------------

    def network_url(self) -> str:
        """Base URL inside the network, as pyiceberg wants it."""
        return f"http://{self.name}:{self.port}"

    def clickhouse_url(self) -> str:
        return self.network_url() + self.clickhouse_path

    def is_ready(self, topology: Topology, node: ServiceNode) -> bool:
        request = urllib.request.Request(
            f"http://127.0.0.1:{node.port(API_OFFSET)}/v1/config",
            headers=self._auth_headers(),
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def tool_env(self, topology: Topology, node: ServiceNode) -> dict[str, str]:
        # pyiceberg reads catalogs from PYICEBERG_CATALOG__<NAME>__<KEY>; "__" nests and "_"
        # becomes "-", so S3__ACCESS_KEY_ID is the property s3.access-key-id.
        prefix = f"PYICEBERG_CATALOG__{self.pyiceberg_name.upper()}__"
        env = {
            prefix + "TYPE": "rest",
            prefix + "URI": self.network_url(),
            prefix + "S3__ENDPOINT": s3.network_endpoint(),
            prefix + "S3__ACCESS_KEY_ID": S3_ROOT_USER,
            prefix + "S3__SECRET_ACCESS_KEY": S3_ROOT_PASSWORD,
            prefix + "S3__REGION": s3.REGION,
        }
        if self.token:
            env[prefix + "TOKEN"] = self.token
        return env

    def describe(self, topology: Topology, node: ServiceNode) -> dict[str, Any]:
        spec = topology.spec
        return {
            "image": self.image,
            "container": f"{spec.project_name}-{self.name}",
            "host": {"endpoint": f"http://127.0.0.1:{node.port(API_OFFSET)}"},
            "network": {"endpoint": self.network_url(), "clickhouse": self.clickhouse_url()},
            "warehouse": self.warehouse_setting(spec),
            "token": self.token,
            "pyiceberg": {
                "catalog": self.pyiceberg_name,
                "python": f"from pyiceberg.catalog import load_catalog; "
                f"catalog = load_catalog({self.pyiceberg_name!r})",
            },
            "sql": {"database": self.database_sql(spec)},
        }

    def database_sql(self, spec: ClusterSpec, database: str | None = None) -> str:
        """``CREATE DATABASE`` for this catalog, with the setting it needs, to paste in a client."""
        return f"SET {DATABASE_SETTING} = 1; {self.create_database_statement(spec, database)}"

    def create_database_statement(self, spec: ClusterSpec, database: str | None = None) -> str:
        """``CREATE DATABASE IF NOT EXISTS`` for this catalog, in the syntax the version needs.

        The engine was ``Iceberg`` before 25.3 and ``DataLakeCatalog`` from then on; both
        need ``DATABASE_SETTING``, which the caller sets.
        """
        engine = "Iceberg" if s3.version_below(spec.version, (25, 3)) else "DataLakeCatalog"
        settings = [
            "catalog_type = 'rest'",
            f"storage_endpoint = '{s3.network_endpoint()}/{spec.effective_buckets[0]}'",
            f"warehouse = '{self.warehouse_setting(spec)}'",
        ]
        if self.token:
            settings.append(f"auth_header = 'Authorization: Bearer {self.token}'")
        name = database or self.name.replace("-", "_")
        quoted = "`" + name.replace("\\", "\\\\").replace("`", "``") + "`"
        return (
            f"CREATE DATABASE IF NOT EXISTS {quoted} ENGINE = {engine}('{self.clickhouse_url()}', "
            f"'{S3_ROOT_USER}', '{S3_ROOT_PASSWORD}') SETTINGS {', '.join(settings)}"
        )


class IcebergRestFixture(RestCatalog):
    name = "iceberg-rest"
    image = "apache/iceberg-rest-fixture:1.9.1"
    port = 8181
    pyiceberg_name = "rest"
    clickhouse_path = "/v1"
    volume = "iceberg-rest-data"

    def warehouse_setting(self, spec: ClusterSpec) -> str:
        return f"s3://{spec.effective_buckets[0]}/"

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        spec = topology.spec
        return {
            "image": self.image,
            "container_name": f"{spec.project_name}-{self.name}",
            "hostname": self.name,
            "environment": {
                "AWS_ACCESS_KEY_ID": S3_ROOT_USER,
                "AWS_SECRET_ACCESS_KEY": S3_ROOT_PASSWORD,
                "AWS_REGION": s3.REGION,
                "CATALOG_WAREHOUSE": self.warehouse_setting(spec),
                "CATALOG_IO__IMPL": "org.apache.iceberg.aws.s3.S3FileIO",
                "CATALOG_S3_ENDPOINT": s3.network_endpoint(),
                "CATALOG_S3_PATH__STYLE__ACCESS": "true",
                # A file, not the default in-memory SQLite: that one is per connection, so
                # concurrent requests see different, empty catalogs.
                "CATALOG_URI": "jdbc:sqlite:file:/data/catalog.db"
                "?journal_mode=WAL&busy_timeout=30000",
            },
            # The fixture image runs as an unprivileged user; root lets it write the volume.
            "user": "root",
            "volumes": [f"{self.volume}:/data"],
            "ports": [f"127.0.0.1:{node.port(API_OFFSET)}:{self.port}"],
            "depends_on": [s3.HOSTNAME],
            "restart": "unless-stopped",
        }

    def volumes(self) -> list[str]:
        return [self.volume]


class IceRestCatalog(RestCatalog):
    name = "ice-rest"
    image = "altinity/ice-rest-catalog:0.16.0"
    port = 5000
    pyiceberg_name = "ice"
    token = "foo"
    volume = "ice-rest-data"
    config_file = "ice-rest-catalog.yaml"

    def warehouse_setting(self, spec: ClusterSpec) -> str:
        return f"s3://{spec.effective_buckets[0]}/"

    def files(self, topology: Topology) -> dict[str, str]:
        spec = topology.spec
        return {
            self.config_file: (
                "uri: jdbc:sqlite:file:/var/lib/ice-rest-catalog/db.sqlite"
                "?journal_mode=WAL&synchronous=OFF&journal_size_limit=500\n"
                f"warehouse: {self.warehouse_setting(spec)}\n"
                "s3:\n"
                f"  endpoint: {s3.network_endpoint()}\n"
                "  pathStyleAccess: true\n"
                f"  accessKeyID: {S3_ROOT_USER}\n"
                f"  secretAccessKey: {S3_ROOT_PASSWORD}\n"
                f"  region: {s3.REGION}\n"
                "bearerTokens:\n"
                f"  - value: {self.token}\n"
            )
        }

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        spec = topology.spec
        config = root / "services" / self.name / self.config_file
        return {
            "image": self.image,
            "container_name": f"{spec.project_name}-{self.name}",
            "hostname": self.name,
            "user": "root",
            "volumes": [
                f"{config}:/etc/ice/ice-rest-catalog.yaml:ro",
                f"{self.volume}:/var/lib/ice-rest-catalog",
            ],
            "ports": [f"127.0.0.1:{node.port(API_OFFSET)}:{self.port}"],
            "depends_on": [s3.HOSTNAME],
            "restart": "unless-stopped",
        }

    def volumes(self) -> list[str]:
        return [self.volume]
