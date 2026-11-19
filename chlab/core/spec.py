"""The declarative description of a cluster.

Everything downstream — config generation, compose rendering, seeding, the web form — is a
function of a ``ClusterSpec``. The CLI and the UI are two ways of filling one in.
"""

from __future__ import annotations

import enum
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from .errors import ChlabError

DEFAULT_CLUSTER_NAME = "default_cluster"
DEFAULT_USER = "default"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")
#: Also the bound that keeps node ports clear of keeper ports; see topology.KEEPER_PORT_OFFSET.
MAX_NODES = 30
#: Bounds the keeper port range, so companion service ports can sit above it.
MAX_KEEPERS = 9
#: Companion services ``--with`` accepts. Each has a module in ``chlab.core.services``.
KNOWN_SERVICES = ("s3", "iceberg-rest", "ice-rest", "spark", "toxiproxy")
#: Services that cannot run without others. Catalogs keep their tables in object storage.
SERVICE_REQUIREMENTS = {
    "iceberg-rest": ("s3",),
    "ice-rest": ("s3",),
    "spark": ("s3",),
    "toxiproxy": ("s3",),
}
#: Services that need at least one of these. Spark writes tables through a catalog.
SERVICE_NEEDS_ONE_OF = {"spark": ("iceberg-rest", "ice-rest")}
DEFAULT_BUCKET = "warehouse"
#: S3 bucket naming rules, minus the ones (no IP-address names) no one hits in a lab.
BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
#: RustFS rejects shorter secrets with "invalid secret key length" — at bucket setup, long
#: after the command that asked for it. Checked here instead.
MIN_S3_SECRET_LENGTH = 8
S3_ACCESS_LEVELS = ("readwrite", "readonly", "writeonly")
#: A ClickHouse config element name. Keys become XML tags, which ElementTree will not validate.
CLUSTER_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
SETTING_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class SpecError(ChlabError):
    pass


class UserRole(enum.StrEnum):
    """Privilege preset applied to a user when the cluster starts."""

    NONE = "none"
    READONLY = "readonly"
    READWRITE = "readwrite"
    ADMIN = "admin"


ROLE_NAMES = frozenset(member.value for member in UserRole)


@dataclass(frozen=True)
class UserSpec:
    name: str
    password: str = ""
    role: str = UserRole.READWRITE.value
    #: Extra grants applied on top of the role, e.g. ``["SELECT ON demo.*"]``.
    grants: tuple[str, ...] = ()
    profile: str = "default"

    def __post_init__(self) -> None:
        # JSON has no tuples, so a spec read back from disk arrives with lists.
        object.__setattr__(self, "grants", tuple(self.grants))
        if self.role not in ROLE_NAMES:
            raise SpecError(
                f"unknown role {self.role!r} for user {self.name!r}; "
                f"expected one of {', '.join(sorted(ROLE_NAMES))}"
            )

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN.value


@dataclass(frozen=True)
class DatabaseSpec:
    name: str
    #: ``Atomic`` or ``Replicated``. Replicated requires Keeper.
    engine: str = "Atomic"


@dataclass(frozen=True)
class S3UserSpec:
    """An extra object-storage identity, for reproducing access-control behaviour."""

    name: str
    secret: str
    #: ``readwrite``, ``readonly`` or ``writeonly``.
    access: str = "readwrite"
    #: Buckets the policy covers. Empty means every bucket of the instance.
    buckets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "buckets", tuple(self.buckets))
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", self.name or ""):
            raise SpecError(f"S3 user name {self.name!r} must be 3-64 letters, digits, '_' or '-'")
        if self.name == S3_ROOT_USER:
            raise SpecError(f"S3 user {S3_ROOT_USER!r} is the root identity and already exists")
        if len(self.secret) < MIN_S3_SECRET_LENGTH:
            raise SpecError(
                f"S3 user {self.name!r} needs a secret of at least {MIN_S3_SECRET_LENGTH} "
                "characters; the storage server rejects shorter ones"
            )
        if self.access not in S3_ACCESS_LEVELS:
            raise SpecError(
                f"unknown S3 access {self.access!r} for {self.name!r}; "
                f"expected one of {', '.join(S3_ACCESS_LEVELS)}"
            )


#: Root credentials of the s3 service. Fixed and well known on purpose: this is a lab, and
#: clickhouse-regression uses the same pair, so its snippets work unchanged.
S3_ROOT_USER = "admin"
S3_ROOT_PASSWORD = "password"


@dataclass(frozen=True)
class ClusterSpec:
    """A named cluster, ready to be launched."""

    name: str
    source: str
    version: str | None = None
    shards: int = 1
    replicas: int = 1
    #: Number of Keeper nodes. ``None`` means "as many as the topology needs".
    keeper_nodes: int | None = None
    cluster_name: str = DEFAULT_CLUSTER_NAME
    users: tuple[UserSpec, ...] = ()
    databases: tuple[DatabaseSpec, ...] = ()
    #: Create the synthetic demo tables after start.
    seed: bool = False
    #: Extra server settings merged into config.d as ``<key>value</key>``.
    settings: dict[str, str] = field(default_factory=dict)
    #: Query-level settings written into the ``default`` settings profile, so they apply to
    #: every session and every user — ``allow_insert_into_iceberg`` and friends, which are
    #: not server settings and are silently ignored in config.d.
    profile_settings: dict[str, str] = field(default_factory=dict)
    #: Companion services started alongside the cluster, from ``KNOWN_SERVICES``.
    services: tuple[str, ...] = ()
    #: Buckets created in the s3 service. Empty means just ``DEFAULT_BUCKET``.
    buckets: tuple[str, ...] = ()
    #: Extra identities in the s3 service, beyond the root user.
    s3_users: tuple[S3UserSpec, ...] = ()
    #: First host port of this instance's allocated block; assigned at creation.
    port_base: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "users", tuple(self.users))
        object.__setattr__(self, "databases", tuple(self.databases))
        # Copied so a caller mutating the dict it passed in cannot change this spec. The
        # dataclass is frozen but that only guards rebinding, not the dict's contents.
        object.__setattr__(self, "settings", dict(self.settings))
        object.__setattr__(self, "profile_settings", dict(self.profile_settings))
        object.__setattr__(self, "services", tuple(self.services))
        object.__setattr__(self, "buckets", tuple(self.buckets))
        object.__setattr__(self, "s3_users", tuple(self.s3_users))

        if not NAME_PATTERN.match(self.name):
            raise SpecError(
                f"instance name {self.name!r} must be lowercase letters, digits, '-' or '_', "
                "starting with a letter or digit, at most 31 characters"
            )
        if not CLUSTER_NAME_PATTERN.match(self.cluster_name):
            # It is an element name in remote_servers and a name in cluster() SQL.
            raise SpecError(
                f"cluster name {self.cluster_name!r} must be letters, digits and '_', "
                "not starting with a digit"
            )
        if self.shards < 1 or self.replicas < 1:
            raise SpecError("shards and replicas must both be at least 1")
        if self.node_count > MAX_NODES:
            raise SpecError(
                f"{self.node_count} nodes exceeds the {MAX_NODES}-node limit chlab launches "
                "at once (also the bound that keeps node ports clear of keeper ports)"
            )
        if self.keeper_nodes is not None and self.keeper_nodes < 0:
            raise SpecError("keeper_nodes cannot be negative")
        if self.effective_keeper_nodes > MAX_KEEPERS:
            raise SpecError(f"at most {MAX_KEEPERS} keeper nodes are supported")
        self._validate_services()
        for key in (*self.settings, *self.profile_settings):
            validate_setting_key(key)
        for database in self.databases:
            if database.engine == "Replicated" and self.effective_keeper_nodes == 0:
                raise SpecError(
                    f"database {database.name!r} uses the Replicated engine, which needs Keeper; "
                    "pass --keeper 1 or more"
                )

    def _validate_services(self) -> None:
        for service in self.services:
            if service not in KNOWN_SERVICES:
                raise SpecError(
                    f"unknown service {service!r}; expected one of {', '.join(KNOWN_SERVICES)}"
                )
        if len(set(self.services)) != len(self.services):
            raise SpecError("each service can be requested only once")
        for service in self.services:
            for required in SERVICE_REQUIREMENTS.get(service, ()):
                if required not in self.services:
                    raise SpecError(f"{service} needs {required}; add --with {required}")
            options = SERVICE_NEEDS_ONE_OF.get(service)
            if options and not any(option in self.services for option in options):
                raise SpecError(
                    f"{service} needs a catalog; add --with {' or --with '.join(options)}"
                )

        if (self.buckets or self.s3_users) and not self.has_service("s3"):
            raise SpecError("--bucket and --s3-user need the s3 service; add --with s3")
        for bucket in self.buckets:
            if not BUCKET_PATTERN.match(bucket):
                raise SpecError(
                    f"invalid bucket name {bucket!r}: 3-63 lowercase letters, digits, '.' or '-'"
                )
        if len(set(self.buckets)) != len(self.buckets):
            raise SpecError("bucket names must be unique")

        names = [user.name for user in self.s3_users]
        if len(set(names)) != len(names):
            raise SpecError("S3 user names must be unique")
        for user in self.s3_users:
            unknown = [bucket for bucket in user.buckets if bucket not in self.effective_buckets]
            if unknown:
                raise SpecError(
                    f"S3 user {user.name!r} refers to bucket(s) {', '.join(unknown)}, which are "
                    "not created; add them with --bucket"
                )

    def has_service(self, name: str) -> bool:
        return name in self.services

    @property
    def effective_buckets(self) -> tuple[str, ...]:
        if not self.has_service("s3"):
            return ()
        return self.buckets or (DEFAULT_BUCKET,)

    @property
    def node_count(self) -> int:
        return self.shards * self.replicas

    @property
    def effective_keeper_nodes(self) -> int:
        """Keeper is implied by replication, but can be requested or refused explicitly."""
        if self.keeper_nodes is not None:
            return self.keeper_nodes
        return 1 if self.replicas > 1 else 0

    @property
    def uses_keeper(self) -> bool:
        return self.effective_keeper_nodes > 0

    @property
    def project_name(self) -> str:
        """Docker Compose project name; namespaces every container and volume."""
        return f"chlab-{self.name}"

    @property
    def network_name(self) -> str:
        """Compose's default network for the project; every service resolves by hostname on it."""
        return f"{self.project_name}_default"

    def with_ports(self, port_base: int) -> ClusterSpec:
        return replace(self, port_base=port_base)

    def with_version(self, version: str) -> ClusterSpec:
        return replace(self, version=version)

    # --- serialization -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ClusterSpec:
        """Rebuild a spec, ignoring fields this version does not know about.

        Specs outlive the code that wrote them — an instance created by a different chlab
        must still be listable and removable rather than crashing the command.
        """
        payload = dict(data)
        payload["users"] = tuple(construct(UserSpec, user) for user in payload.get("users") or ())
        payload["databases"] = tuple(
            construct(DatabaseSpec, database) for database in payload.get("databases") or ()
        )
        payload["s3_users"] = tuple(
            construct(S3UserSpec, user) for user in payload.get("s3_users") or ()
        )
        return construct(cls, payload)


def construct(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, dropping keys it does not declare.

    Used everywhere untrusted or older/newer persisted state is turned back into objects, so
    an unrecognised field is ignored rather than raising ``TypeError`` at the constructor.
    """
    known = set(cls.__dataclass_fields__)
    return cls(**{key: value for key, value in data.items() if key in known})


def validate_setting_key(key: str) -> str:
    """Reject setting names that would produce malformed XML.

    Keys become element names in ``config.d``. ElementTree does not validate them, so an
    invalid key yields a config the server rejects at startup — surfacing as a health-check
    timeout minutes later, far from the actual mistake.
    """
    if not SETTING_KEY_PATTERN.match(key):
        raise SpecError(
            f"invalid setting name {key!r}; expected a ClickHouse config element name "
            "(letters, digits, '_', '.' and '-', not starting with a digit)"
        )
    return key


_VERSION_IN_TEXT = re.compile(r"\d+\.\d+\.\d+(?:\.\d+)?")


def suggest_name(source: str, *, taken: set[str] | None = None) -> str:
    """Propose an instance name for a source, so ``--name`` is optional.

    Version numbers make the most recognisable names: a source mentioning 26.3.12.20001
    becomes ``ch-26-3-12-20001``. Anything else falls back to its sanitised basename.
    """
    taken = taken or set()
    match = _VERSION_IN_TEXT.search(source)
    if match:
        base = "ch-" + match.group(0).replace(".", "-")
    else:
        tail = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        base = re.sub(r"[^a-z0-9]+", "-", tail.lower()).strip("-")[:24] or "chlab"
        if not base[0].isalnum():
            base = f"ch-{base}"

    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise SpecError(f"could not find an unused name based on {base!r}; pass --name")


def parse_user(text: str) -> UserSpec:
    """Parse a ``--user name[:password][:role]`` argument.

    The role is recognised only as a trailing field holding a known role name, so passwords
    containing colons still work: ``dev:p:ss`` is the password ``p:ss``, while ``dev:p:admin``
    is the password ``p`` with the admin role.
    """
    parts = text.split(":")
    name = parts[0].strip()
    if not name:
        raise SpecError(f"user {text!r} has no name")

    rest = parts[1:]
    role = UserRole.READWRITE.value
    if len(rest) > 1 and rest[-1].strip().lower() in ROLE_NAMES:
        role = rest[-1].strip().lower()
        rest = rest[:-1]

    return UserSpec(name=name, password=":".join(rest), role=role)


def parse_grant(text: str) -> tuple[str, str]:
    """Parse a ``--grant user:PRIVILEGES ON target`` argument."""
    user, separator, grant = text.partition(":")
    if not separator or not user.strip() or not grant.strip():
        raise SpecError(f"could not parse grant {text!r}; expected user:PRIVILEGES ON target")
    return user.strip(), grant.strip()


def parse_database(text: str) -> DatabaseSpec:
    """Parse a ``--database name[:engine]`` argument."""
    name, _, engine = text.partition(":")
    name = name.strip()
    if not name:
        raise SpecError(f"database {text!r} has no name")
    engine = (engine or "Atomic").strip()
    if engine not in {"Atomic", "Replicated"}:
        raise SpecError(f"unsupported database engine {engine!r}; use Atomic or Replicated")
    return DatabaseSpec(name=name, engine=engine)


def parse_setting(text: str) -> tuple[str, str]:
    """Parse a ``--setting key=value`` argument."""
    key, separator, value = text.partition("=")
    if not separator:
        raise SpecError(f"setting {text!r} must be written key=value")
    return validate_setting_key(key.strip()), value.strip()


def parse_s3_user(text: str) -> S3UserSpec:
    """Parse a ``--s3-user name:secret[:access[:bucket,bucket]]`` argument.

    ``reader:readerpass:readonly:warehouse`` can read the ``warehouse`` bucket and nothing
    else. Secrets cannot contain ``:``.
    """
    parts = text.split(":")
    if len(parts) < 2 or len(parts) > 4:
        raise SpecError(
            f"could not parse S3 user {text!r}; expected name:secret[:access[:bucket,bucket]]"
        )
    name, secret = parts[0].strip(), parts[1]
    access = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "readwrite"
    buckets = tuple(b.strip() for b in parts[3].split(",") if b.strip()) if len(parts) > 3 else ()
    return S3UserSpec(name=name, secret=secret, access=access, buckets=buckets)
