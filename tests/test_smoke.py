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
