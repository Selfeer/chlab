"""End-to-end smoke test against a real ClickHouse container.

The only test that exercises the generated ``config.xml``, the base image and the compose file
against an actual server — everything else stops at the boundary. Skipped unless
``--run-docker`` is passed, because it pulls an image and takes a minute.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from chlab.core.cache import Cache
from chlab.core.registry import Registry
from chlab.core.runtime import Runtime
from chlab.core.spec import ClusterSpec, UserSpec

pytestmark = pytest.mark.docker

IMAGE = "docker://clickhouse/clickhouse-server:24.8"


@pytest.fixture
def runtime(tmp_path: Path) -> Runtime:
    return Runtime(registry=Registry(tmp_path / "state"), cache=Cache(tmp_path / "cache"))


def test_launch_query_and_tear_down(runtime: Runtime):
    spec = ClusterSpec(
        name="smoke",
        source=IMAGE,
        shards=2,
        replicas=1,
        seed=True,
        users=(UserSpec("reader", "pw", role="readonly"),),
    )
    try:
        status = runtime.up(spec)
        assert status.state == "running"
        assert status.spec.version and status.spec.version.startswith("24.8")

        # ClickHouse returns UInt64 as a JSON string, to avoid precision loss in JS clients.
        total = runtime.query("smoke", "SELECT count() FROM demo.events")
        assert int(total.rows[0][0]) == 100000

        # The real test of the sharding key: a predicate ON the sharding key lets ClickHouse
        # consult only the shard that key selects. If the seed had placed rows by some other
        # rule, this query would silently miss the ones sitting on the "wrong" shard. A bare
        # count() proves nothing here — with no predicate, no shard is ever skipped.
        sample = runtime.query("smoke", "SELECT user_id FROM demo.events LIMIT 20")
        assert sample.rows, "expected sample rows"

        for (user_id,) in sample.rows:
            predicate = f"SELECT count() FROM demo.events WHERE user_id = {int(user_id)}"
            honest = runtime.query("smoke", predicate)
            skipped = runtime.query(
                "smoke", predicate, settings={"optimize_skip_unused_shards": "1"}
            )
            assert int(skipped.rows[0][0]) == int(honest.rows[0][0]) > 0, (
                f"user_id {user_id} is not on the shard its sharding key selects"
            )

        per_node = runtime.query_all("smoke", "SELECT count() FROM demo.events_local")
        assert sum(int(result.rows[0][0]) for result in per_node) == 100000

        as_reader = runtime.query("smoke", "SELECT currentUser()", user="reader", password="pw")
        assert as_reader.rows == [["reader"]]
    finally:
        runtime.destroy("smoke")


def test_s3_service_round_trip(runtime: Runtime):
    """Object storage, named collections, request counts and a scoped identity, end to end."""
    from chlab.core.query import QueryError
    from chlab.core.spec import S3UserSpec

    spec = ClusterSpec(
        name="smoke-s3",
        source=IMAGE,
        services=("s3",),
        buckets=("warehouse", "other"),
        s3_users=(S3UserSpec("reader", "readerpass", "readonly", ("warehouse",)),),
    )
    try:
        runtime.up(spec)
        runtime.query(
            "smoke-s3",
            "INSERT INTO FUNCTION s3(s3, filename = 'd.parquet') SELECT number FROM numbers(1000)",
        )
        result = runtime.query(
            "smoke-s3", "SELECT sum(number) FROM s3(s3, filename = 'd.parquet')", query_id="smoke-q"
        )
        assert int(result.rows[0][0]) == 499500
        assert runtime.profile_events("smoke-s3", "smoke-q", "^S3GetObject$")["S3GetObject"] > 0

        url = "http://s3:9000/{}/d.parquet"
        readable = runtime.query(
            "smoke-s3",
            f"SELECT count() FROM s3('{url.format('warehouse')}', 'reader', 'readerpass')",
        )
        assert int(readable.rows[0][0]) == 1000
        runtime.query(
            "smoke-s3", "INSERT INTO FUNCTION s3(s3_other, filename='d.parquet') SELECT 1"
        )
        with pytest.raises(QueryError) as denied:
            runtime.query(
                "smoke-s3",
                f"SELECT count() FROM s3('{url.format('other')}', 'reader', 'readerpass')",
            )
        assert denied.value.code == 499  # S3_ERROR: the storage refused the scoped identity
    finally:
        runtime.destroy("smoke-s3")


def test_iceberg_table_written_by_pyiceberg_is_read_by_clickhouse(runtime: Runtime, tmp_path: Path):
    """The tools container writes through the REST catalog; ClickHouse reads the files."""
    import subprocess

    from chlab.core.image import ensure_tools_image
    from chlab.core.services.s3 import iceberg_without_catalog_sql
    from chlab.core.tools import tools_command

    spec = ClusterSpec(name="smoke-ice", source=IMAGE, services=("s3", "iceberg-rest"))
    script = """
import pyarrow as pa
from chlab_tools import catalog, BUCKET
cat = catalog()
cat.create_namespace_if_not_exists("demo")
table = cat.create_table("demo.t", schema=pa.schema([("id", pa.int64())]), location=f"s3://{BUCKET}/t")
table.append(pa.table({"id": pa.array(range(42), pa.int64())}))
"""
    try:
        status = runtime.up(spec)
        result = subprocess.run(
            tools_command(
                status.spec,
                ["python", "-"],
                image=ensure_tools_image(),
                workdir=tmp_path,
                interactive=True,
            ),
            input=script,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr
        read = iceberg_without_catalog_sql(status.spec)["iceberg_read"].replace(
            "path/to/table", "t"
        )
        assert int(runtime.query("smoke-ice", read.replace("*", "count()")).rows[0][0]) == 42
    finally:
        runtime.destroy("smoke-ice")


def test_a_reproduction_runs_end_to_end(runtime: Runtime, tmp_path: Path):
    """SQL with a reference, an expected error, a node script and a restart, on fresh instances."""
    from chlab.core import repro

    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "repro.yaml").write_text("title: smoke\n")
    (directory / "01-create.sql").write_text(
        "CREATE TABLE t (a Int32) ENGINE = MergeTree ORDER BY a; INSERT INTO t VALUES (1), (2);"
    )
    (directory / "02-missing.sql").write_text("-- chlab: expect error 60\nSELECT * FROM nope;")
    (directory / "03-files.sh").write_text(
        "# chlab: node clickhouse1\n# chlab: expect contains clickhouse-server\nls /etc\n"
    )
    (directory / "04-restart.chlab").write_text("restart\n")
    (directory / "05-count.sql").write_text("SELECT count() FROM t;")
    (directory / "05-count.reference").write_text("2\n")

    run = repro.Runner(runtime).run(repro.load(directory), [IMAGE])
    build = run.builds[0]
    assert build.error is None, build.error
    assert [step.passed for step in build.steps] == [True] * 5, [s.failures for s in build.steps]
    assert build.steps[1].error_code == 60
    assert not runtime.registry.exists(build.instance)


def test_toxiproxy_slows_and_breaks_proxied_reads_only(runtime: Runtime):
    from chlab.core.query import QueryError
    from chlab.core.services import toxiproxy
    from chlab.core.topology import build_topology

    spec = ClusterSpec(name="smoke-proxy", source=IMAGE, services=("s3", "toxiproxy"))
    try:
        status = runtime.up(spec)
        node = build_topology(status.spec).companion("toxiproxy")
        runtime.query(
            "smoke-proxy",
            "INSERT INTO FUNCTION s3(s3, filename='d.csv') SELECT number FROM numbers(10)",
        )
        read = "SELECT count() FROM s3({}, filename = 'd.csv')"
        toxiproxy.set_toxic(node, "latency", 400)
        slow = runtime.query("smoke-proxy", read.format("s3_proxy"))
        direct = runtime.query("smoke-proxy", read.format("s3"))
        assert int(slow.rows[0][0]) == int(direct.rows[0][0]) == 10
        assert slow.elapsed > 0.4 > direct.elapsed

        # Storage that is gone must fail the read. How fast depends on the build's retry
        # policy (26.3 returns S3_ERROR at once, 24.8 retries), so only failure is asserted.
        toxiproxy.set_enabled(node, False)
        with pytest.raises(QueryError):
            runtime.query(
                "smoke-proxy",
                read.format("s3_proxy"),
                settings={
                    "s3_max_single_read_retries": "0",
                    "s3_retry_attempts": "0",
                    "max_execution_time": "10",
                },
            )
    finally:
        runtime.destroy("smoke-proxy")


def test_spark_writes_a_table_clickhouse_reads(runtime: Runtime):
    import subprocess

    from chlab.core.services.s3 import iceberg_without_catalog_sql
    from chlab.core.services.spark import spark_sql_command

    spec = ClusterSpec(name="smoke-spark", source=IMAGE, services=("s3", "iceberg-rest", "spark"))
    statements = """
CREATE NAMESPACE IF NOT EXISTS demo;
CREATE TABLE demo.t (id BIGINT) USING iceberg LOCATION 's3://warehouse/t'
  TBLPROPERTIES ('format-version'='2', 'write.delete.mode'='copy-on-write');
INSERT INTO demo.t SELECT id FROM range(50);
DELETE FROM demo.t WHERE id < 8;
"""
    try:
        status = runtime.up(spec)
        result = subprocess.run(
            spark_sql_command(status.spec), input=statements, text=True, capture_output=True
        )
        assert result.returncode == 0, result.stderr[-2000:]
        read = iceberg_without_catalog_sql(status.spec)["iceberg_read"].replace(
            "path/to/table", "t"
        )
        assert int(runtime.query("smoke-spark", read.replace("*", "count()")).rows[0][0]) == 42
    finally:
        runtime.destroy("smoke-spark")
