"""Everything chlab creates inside a cluster once it is running.

Users, databases and demo tables are all created as SQL objects rather than declared in XML.
Users in ``users.xml`` are read-only to the RBAC system, so ``GRANT`` against them fails; SQL
users live in the local access directory and behave normally. The same DDL is run on every
node — ``ReplicatedMergeTree`` and the ``Replicated`` database engine are designed for each
replica to register itself, so no ``ON CLUSTER`` and no DDL queue is involved.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .spec import ClusterSpec, UserRole, UserSpec
from .topology import Topology

DEMO_DATABASE = "demo"
DEMO_TABLE = "events_local"
DEMO_DISTRIBUTED = "events"
DEMO_ROWS = 100_000
#: Sharding expression of the Distributed table. The seed inserts must place each row on the
#: shard this expression selects, or `optimize_skip_unused_shards=1` silently drops rows —
#: a trap in a tool whose job is checking whether a build behaves correctly.
SHARDING_KEY = "cityHash64(user_id)"
#: How user_id is generated, so the insert can evaluate SHARDING_KEY over the same values.
USER_ID_EXPRESSION = "toUInt32(cityHash64(number) % 10000)"


@dataclass
class InitPlan:
    """DDL to run after the cluster reports healthy."""

    #: Statements to run on every node.
    common: list[str] = field(default_factory=list)
    #: Demo table DDL, also run on every node.
    seed_ddl: list[str] = field(default_factory=list)
    #: Node name -> insert statement. Only the first replica of each shard inserts; the
    #: others receive the rows through replication.
    seed_inserts: dict[str, str] = field(default_factory=dict)

    @property
    def has_seed(self) -> bool:
        return bool(self.seed_ddl)


def build_plan(topology: Topology) -> InitPlan:
    spec = topology.spec
    plan = InitPlan()

    for user in spec.users:
        plan.common.extend(user_statements(user))
    for database in spec.databases:
        plan.common.append(database_statement(database.name, database.engine))

    if spec.seed:
        plan.seed_ddl = seed_ddl(spec)
        plan.seed_inserts = seed_inserts(topology)

    return plan


#: Privileges each role preset grants on ``*.*``.
ROLE_GRANTS = {
    UserRole.NONE.value: None,
    UserRole.READONLY.value: "SELECT, SHOW",
    UserRole.READWRITE.value: "SELECT, INSERT, ALTER, CREATE, DROP, TRUNCATE, OPTIMIZE, SHOW, dictGet",
    # Not `GRANT ALL`: the default user does not itself hold every privilege ALL covers
    # (SHOW NAMED COLLECTIONS SECRETS, among others) and cannot grant what it lacks.
    # CURRENT GRANTS passes on exactly what the granting user has.
    UserRole.ADMIN.value: "CURRENT GRANTS",
}


def user_statements(user: UserSpec) -> list[str]:
    # sha256_password rather than plaintext_password: the server stores only a hash, so the
    # cleartext never reaches its access storage. Clients still authenticate with the password
    # the user chose — ClickHouse hashes it on arrival.
    identified = (
        f"IDENTIFIED WITH sha256_password BY {_quote(user.password)}"
        if user.password
        else "IDENTIFIED WITH no_password"
    )
    statements = [f"CREATE USER IF NOT EXISTS {_ident(user.name)} {identified}"]

    privileges = ROLE_GRANTS.get(user.role)
    if privileges:
        suffix = " WITH GRANT OPTION" if user.is_admin else ""
        statements.append(f"GRANT {privileges} ON *.* TO {_ident(user.name)}{suffix}")
    # Grants are interpolated verbatim, on purpose: the whole point is arbitrary privilege
    # expressions, and anyone who can pass one can already run arbitrary SQL. The _ident and
    # _quote helpers below cover the values that are *not* meant to be expressions.
    for grant in user.grants:
        statements.append(f"GRANT {grant} TO {_ident(user.name)}")

    return statements


def database_statement(name: str, engine: str = "Atomic") -> str:
    if engine == "Replicated":
        return (
            f"CREATE DATABASE IF NOT EXISTS {_ident(name)} "
            f"ENGINE = Replicated('/clickhouse/databases/{name}', '{{shard}}', '{{replica}}')"
        )
    return f"CREATE DATABASE IF NOT EXISTS {_ident(name)}"


def seed_ddl(spec: ClusterSpec) -> list[str]:
    if spec.replicas > 1:
        engine = (
            f"ReplicatedMergeTree('/clickhouse/tables/{{shard}}/{DEMO_DATABASE}/{DEMO_TABLE}', "
            "'{replica}')"
        )
    else:
        engine = "MergeTree()"

    return [
        database_statement(DEMO_DATABASE),
        f"""CREATE TABLE IF NOT EXISTS {DEMO_DATABASE}.{DEMO_TABLE}
(
    event_time DateTime,
    user_id UInt32,
    event_type LowCardinality(String),
    country LowCardinality(String),
    value Float64
)
ENGINE = {engine}
PARTITION BY toYYYYMM(event_time)
ORDER BY (event_type, user_id, event_time)""",
        f"""CREATE TABLE IF NOT EXISTS {DEMO_DATABASE}.{DEMO_DISTRIBUTED}
AS {DEMO_DATABASE}.{DEMO_TABLE}
ENGINE = Distributed({_quote(spec.cluster_name)}, {DEMO_DATABASE}, {DEMO_TABLE}, {SHARDING_KEY})""",
    ]


def seed_inserts(topology: Topology, rows: int = DEMO_ROWS) -> dict[str, str]:
    """One insert per shard, run on that shard's first replica.

    Each shard takes a disjoint slice of the generated rows, selected by the *same* expression
    the Distributed table shards on. Splitting by `number % shards` instead would be simpler
    and would still total correctly, but rows would sit on shards the sharding key says they
    are not on, and `optimize_skip_unused_shards=1` would then return partial results.
    """
    shards = topology.spec.shards
    placement = SHARDING_KEY.replace("user_id", USER_ID_EXPRESSION)
    inserts = {}
    for shard_nodes in topology.shard_layout():
        if not shard_nodes:
            continue
        writer = shard_nodes[0]
        inserts[writer.name] = f"""INSERT INTO {DEMO_DATABASE}.{DEMO_TABLE}
SELECT
    now() - toIntervalSecond(intDiv(number, 4)) AS event_time,
    {USER_ID_EXPRESSION} AS user_id,
    ['view', 'click', 'purchase', 'signup'][toUInt8(number %% 4) + 1] AS event_type,
    ['GE', 'US', 'DE', 'NL', 'JP'][toUInt8(cityHash64(number, 2) %% 5) + 1] AS country,
    round(cityHash64(number, 1) %% 100000 / 100, 2) AS value
FROM numbers({rows})
WHERE {placement} %% {shards} = {writer.shard - 1}""".replace("%%", "%")
    return inserts


def _ident(name: str) -> str:
    """Backtick-quote an identifier."""
    return "`" + name.replace("`", "``") + "`"


def _quote(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


#: Public name for other modules that build SQL string literals.
quote = _quote
