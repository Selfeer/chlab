"""Spark with Iceberg, for the tables pyiceberg cannot write.

pyiceberg covers most of what a reproduction needs; Spark is for the rest — format v3,
deletion vectors, merge-on-read deletes and updates, ``MERGE INTO``, maintenance procedures
(``rewrite_data_files``, ``expire_snapshots``). Its catalogs are the instance's REST catalogs,
under the same names pyiceberg uses (``rest``, ``ice``), so a table written by one is read by
the other and by ClickHouse.

The image is the one clickhouse-regression uses. It bundles Iceberg 1.8.1, whose v3 tables
predate the final spec and are rejected by the 1.9.1 REST catalog, so — as the regression
suite does — the 1.9.1 runtime is mounted over the 1.8.1 jars. Those are fetched once from
Maven Central into the chlab cache and checked against pinned SHA-256 sums.

Spark runs in local mode, started per statement batch by ``spark-sql``: 20–40 seconds of JVM
start each time, so batch statements into one call.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from ..cache import Cache
from ..download import download
from ..events import EventCallback
from ..spec import S3_ROOT_PASSWORD, S3_ROOT_USER, ClusterSpec
from ..topology import ServiceNode, Topology
from . import s3
from .base import Service
from .catalogs import RestCatalog

#: tabulario/spark-iceberg, pinned by digest: its only tag is a moving `latest`.
IMAGE = (
    "tabulario/spark-iceberg@sha256:"
    "790a72e744e0d09f161e1b4c7c2f79625cc598cf0476da4703e62f1f4e023460"
)
SPARK_SQL = "/opt/spark/bin/spark-sql"
JARS_DIR = "/opt/spark/jars"
CONFIG_PATH = "/opt/spark/conf/spark-defaults.conf"
MAVEN = "https://repo1.maven.org/maven2/org/apache/iceberg"

#: (file in the image to replace, Maven path of the replacement, its SHA-256).
JAR_OVERLAYS = (
    (
        "iceberg-spark-runtime-3.5_2.12-1.8.1.jar",
        "iceberg-spark-runtime-3.5_2.12/1.9.1/iceberg-spark-runtime-3.5_2.12-1.9.1.jar",
        "081a420a912e1f926890b2ceb345d3c234903f04e6a7f45b52615a1f708eb8da",
    ),
    (
        "iceberg-aws-bundle-1.8.1.jar",
        "iceberg-aws-bundle/1.9.1/iceberg-aws-bundle-1.9.1.jar",
        "ea5b6fec1b547e2fe936f804c737943f5ee322d038560f63a8261deb766def1b",
    ),
)

#: Table properties for a format-v3, merge-on-read table: deletes become deletion vectors.
V3_MERGE_ON_READ = {
    "format-version": "3",
    "write.delete.mode": "merge-on-read",
    "write.update.mode": "merge-on-read",
    "write.merge.mode": "merge-on-read",
}


class Spark(Service):
    name = "spark"

    def catalogs(self, topology: Topology) -> list[RestCatalog]:
        from . import active

        return [service for service, _ in active(topology) if isinstance(service, RestCatalog)]

    # --- jars ----------------------------------------------------------------------------

    def jars_dir(self, root: Path) -> Path:
        return root / "services" / self.name / "jars"

    def prepare(self, topology: Topology, root: Path, cache: Cache, notify: EventCallback) -> None:
        target = self.jars_dir(root)
        target.mkdir(parents=True, exist_ok=True)
        for replaced, maven_path, sha256 in JAR_OVERLAYS:
            filename = maven_path.rsplit("/", 1)[1]
            cached = cache.root / "jars" / filename
            if not cached.exists():
                notify(f"downloading {filename}")
            download(f"{MAVEN}/{maven_path}", cached, sha256=sha256)
            _link_or_copy(cached, target / replaced)

    # --- compose -------------------------------------------------------------------------

    def files(self, topology: Topology) -> dict[str, str]:
        return {"spark-defaults.conf": spark_defaults(topology.spec, self.catalogs(topology))}

    def compose_service(self, topology: Topology, node: ServiceNode, root: Path) -> dict[str, Any]:
        spec = topology.spec
        config = root / "services" / self.name / "spark-defaults.conf"
        jars = self.jars_dir(root)
        return {
            "image": IMAGE,
            "container_name": f"{spec.project_name}-{self.name}",
            "hostname": self.name,
            # The image's entrypoint starts a standalone cluster, a history server and a
            # notebook. None of that is needed: spark-sql runs in local mode on demand.
            "entrypoint": ["tail", "-f", "/dev/null"],
            "environment": {
                "AWS_ACCESS_KEY_ID": S3_ROOT_USER,
                "AWS_SECRET_ACCESS_KEY": S3_ROOT_PASSWORD,
                "AWS_REGION": s3.REGION,
            },
            "volumes": [
                f"{config}:{CONFIG_PATH}:ro",
                *(
                    f"{jars / replaced}:{JARS_DIR}/{replaced}:ro"
                    for replaced, _maven, _sha in JAR_OVERLAYS
                ),
            ],
            "depends_on": [s3.HOSTNAME, *(catalog.name for catalog in self.catalogs(topology))],
            "restart": "unless-stopped",
        }

    def is_ready(self, topology: Topology, node: ServiceNode) -> bool:
        container = f"{topology.spec.project_name}-{self.name}"
        try:
            result = subprocess.run(
                ["docker", "exec", container, "test", "-x", SPARK_SQL],
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def describe(self, topology: Topology, node: ServiceNode) -> dict[str, Any]:
        spec = topology.spec
        catalogs = self.catalogs(topology)
        default = catalogs[0].pyiceberg_name
        properties = ", ".join(f"'{key}' = '{value}'" for key, value in V3_MERGE_ON_READ.items())
        return {
            "image": IMAGE,
            "container": f"{spec.project_name}-{self.name}",
            "host": {"endpoint": f'chlab spark {spec.name} "<SQL>"'},
            "network": {"endpoint": self.name},
            "catalogs": [catalog.pyiceberg_name for catalog in catalogs],
            "default_catalog": default,
            "iceberg": "1.9.1",
            "sql": {
                "v3_table": (
                    f"CREATE TABLE {default}.demo.t (id BIGINT, s STRING) USING iceberg "
                    f"TBLPROPERTIES ({properties})"
                ),
                "delete": f"DELETE FROM {default}.demo.t WHERE id % 2 = 0",
                "compact": f"CALL {default}.system.rewrite_data_files('demo.t')",
            },
        }


def spark_defaults(spec: ClusterSpec, catalogs: list[RestCatalog]) -> str:
    lines = [
        (
            "spark.sql.extensions",
            "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        ),
        ("spark.sql.catalogImplementation", "in-memory"),
        ("spark.sql.defaultCatalog", catalogs[0].pyiceberg_name),
        ("spark.ui.enabled", "false"),
        ("spark.eventLog.enabled", "false"),
    ]
    for catalog in catalogs:
        prefix = f"spark.sql.catalog.{catalog.pyiceberg_name}"
        lines += [
            (prefix, "org.apache.iceberg.spark.SparkCatalog"),
            (f"{prefix}.type", "rest"),
            (f"{prefix}.uri", catalog.network_url()),
            (f"{prefix}.warehouse", catalog.warehouse_setting(spec)),
            (f"{prefix}.io-impl", "org.apache.iceberg.aws.s3.S3FileIO"),
            (f"{prefix}.s3.endpoint", s3.network_endpoint()),
            # RustFS rejects virtual-host requests.
            (f"{prefix}.s3.path-style-access", "true"),
        ]
        if catalog.token:
            lines.append((f"{prefix}.token", catalog.token))
    return "".join(f"{key} {value}\n" for key, value in lines)


def spark_sql_command(spec: ClusterSpec) -> list[str]:
    """``docker exec`` running the statements fed on stdin through ``spark-sql -S -f``.

    spark-sql reads a file, not stdin, so the statements land in a temporary file first.
    """
    script = f'f=$(mktemp) && cat > "$f" && exec {SPARK_SQL} -S -f "$f"'
    return ["docker", "exec", "-i", f"{spec.project_name}-spark", "sh", "-c", script]


def _link_or_copy(source: Path, target: Path) -> None:
    """Hard-link a cached jar into the instance, copying where links are not possible."""
    if target.exists():
        target.unlink()
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)
