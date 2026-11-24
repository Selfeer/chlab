from __future__ import annotations

import re

from chlab.core import seed
from chlab.core.spec import ClusterSpec, DatabaseSpec, UserSpec
from chlab.core.topology import build_topology


def topology(shards=2, replicas=2, keeper_nodes=1, **overrides):
    return build_topology(
        ClusterSpec(
            name="t",
            source="/x",
            shards=shards,
            replicas=replicas,
            keeper_nodes=keeper_nodes,
            port_base=21000,
            **overrides,
        )
    )


# --- users -------------------------------------------------------------------------------


def test_user_without_a_password_is_created_with_no_password():
    statements = seed.user_statements(UserSpec("dev"))
    assert "IDENTIFIED WITH no_password" in statements[0]


def test_user_with_a_password_is_stored_hashed():
    """The server keeps only a hash; the user still logs in with the password they chose."""
    statements = seed.user_statements(UserSpec("dev", "secret"))
    assert "IDENTIFIED WITH sha256_password BY 'secret'" in statements[0]
    assert "plaintext_password" not in statements[0]


def test_admin_uses_current_grants_rather_than_all():
    """`GRANT ALL` fails: the default user cannot grant privileges it does not itself hold."""
    statements = seed.user_statements(UserSpec("admin", role="admin"))
    assert any("GRANT CURRENT GRANTS ON *.*" in statement for statement in statements)
    assert any("WITH GRANT OPTION" in statement for statement in statements)
    assert not any("GRANT ALL" in statement for statement in statements)


def test_readonly_gets_select_but_no_grant_option():
    statements = seed.user_statements(UserSpec("ro", role="readonly"))
    grant = statements[1]
    assert grant.startswith("GRANT SELECT, SHOW ON *.*")
    assert "WITH GRANT OPTION" not in grant


def test_readwrite_can_create_and_insert():
    grant = seed.user_statements(UserSpec("dev", role="readwrite"))[1]
    for privilege in ("SELECT", "INSERT", "CREATE", "DROP"):
        assert privilege in grant


def test_role_none_grants_nothing():
    assert len(seed.user_statements(UserSpec("nobody", role="none"))) == 1


def test_explicit_grants_are_appended():
    statements = seed.user_statements(UserSpec("ro", role="none", grants=("SELECT ON demo.*",)))
    assert statements[-1] == "GRANT SELECT ON demo.* TO `ro`"


def test_identifiers_and_passwords_are_escaped():
    statements = seed.user_statements(UserSpec("we`ird", "it's"))
    assert "`we``ird`" in statements[0]
    assert "\\'" in statements[0]


# --- databases ---------------------------------------------------------------------------


def test_atomic_database_statement():
    assert seed.database_statement("demo") == "CREATE DATABASE IF NOT EXISTS `demo`"


def test_replicated_database_uses_macros():
    statement = seed.database_statement("demo", "Replicated")
    assert "ENGINE = Replicated('/clickhouse/databases/demo', '{shard}', '{replica}')" in statement


# --- demo data ---------------------------------------------------------------------------


def test_replicated_topologies_get_replicated_tables():
    ddl = "\n".join(seed.seed_ddl(topology(replicas=2).spec))
    assert "ReplicatedMergeTree" in ddl


def test_single_replica_topologies_get_plain_mergetree():
    ddl = "\n".join(seed.seed_ddl(topology(shards=2, replicas=1, keeper_nodes=0).spec))
    assert "ReplicatedMergeTree" not in ddl
    assert "ENGINE = MergeTree()" in ddl


def test_distributed_table_targets_the_configured_cluster():
    ddl = "\n".join(seed.seed_ddl(topology(cluster_name="my_cluster").spec))
    assert "Distributed('my_cluster', demo, events_local" in ddl


def test_only_the_first_replica_of_each_shard_inserts():
    """The other replicas receive the rows through replication, not a second insert."""
    inserts = seed.seed_inserts(topology(shards=2, replicas=2))
    assert set(inserts) == {"clickhouse1", "clickhouse3"}


def test_shards_take_disjoint_slices_of_the_generated_rows():
    inserts = seed.seed_inserts(topology(shards=3, replicas=1, keeper_nodes=0))
    remainders = sorted(
        int(re.search(r"% 3 = (\d+)", statement).group(1)) for statement in inserts.values()
    )
    assert remainders == [0, 1, 2]


def test_inserts_place_rows_where_the_sharding_key_says_they_are():
    """Otherwise optimize_skip_unused_shards=1 silently returns partial results."""
    spec = topology(shards=2, replicas=1, keeper_nodes=0).spec
    ddl = "\n".join(seed.seed_ddl(spec))
    assert f", {seed.SHARDING_KEY})" in ddl

    placement = seed.SHARDING_KEY.replace("user_id", seed.USER_ID_EXPRESSION)
    for statement in seed.seed_inserts(topology(shards=2, replicas=1, keeper_nodes=0)).values():
        assert placement in statement
        # The value inserted into user_id must be the one the placement expression hashes.
        assert f"{seed.USER_ID_EXPRESSION} AS user_id" in statement


def test_insert_row_count_is_configurable():
    statement = next(iter(seed.seed_inserts(topology(), rows=42).values()))
    assert "numbers(42)" in statement


# --- plan --------------------------------------------------------------------------------


def test_plan_is_empty_without_users_databases_or_seed():
    plan = seed.build_plan(topology())
    assert not plan.common and not plan.has_seed


def test_plan_collects_users_and_databases():
    result = topology(
        users=(UserSpec("dev", "pw"),),
        databases=(DatabaseSpec("analytics"),),
    )
    plan = seed.build_plan(result)
    assert any("CREATE USER" in statement for statement in plan.common)
    assert any("CREATE DATABASE IF NOT EXISTS `analytics`" in s for s in plan.common)


def test_catalog_database_is_planned_with_its_setting():
    """A DataLakeCatalog database on every node, created with the setting it needs."""
    plan = seed.build_plan(
        topology(
            services=("s3", "iceberg-rest"),
            databases=(DatabaseSpec("rest", "iceberg-rest"),),
            version="26.8.1.1",
        )
    )
    assert not plan.common
    (statement,) = plan.catalog_databases
    assert statement.startswith("CREATE DATABASE IF NOT EXISTS `rest` ENGINE = DataLakeCatalog(")
    assert "'http://iceberg-rest:8181/v1'" in statement
    assert "SET " not in statement  # one statement: HTTP runs one at a time
    assert plan.catalog_settings == {"allow_experimental_database_iceberg": "1"}


def test_catalog_database_uses_the_iceberg_engine_before_25_3():
    plan = seed.build_plan(
        topology(
            services=("s3", "ice-rest"),
            databases=(DatabaseSpec("ice", "ice-rest"),),
            version="24.8.1.1",
        )
    )
    assert "ENGINE = Iceberg(" in plan.catalog_databases[0]
    assert "auth_header = 'Authorization: Bearer" in plan.catalog_databases[0]


def test_plan_includes_demo_data_only_when_asked():
    assert not seed.build_plan(topology()).has_seed
    assert seed.build_plan(topology(seed=True)).has_seed
