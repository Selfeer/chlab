"""The declarative description of a cluster.

Everything downstream — config generation, compose rendering, seeding, the web form — is a
function of a ``ClusterSpec``. The CLI and the UI are two ways of filling one in.
"""

from __future__ import annotations

import enum
import re
from dataclasses import asdict, dataclass, field, replace
from typing import Any

DEFAULT_CLUSTER_NAME = "default_cluster"
DEFAULT_USER = "default"
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")


class SpecError(ValueError):
    pass


class UserRole(str, enum.Enum):
    """Privilege preset applied to a user when the cluster starts."""

    NONE = "none"
    READONLY = "readonly"
    READWRITE = "readwrite"
    ADMIN = "admin"


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
        if self.role not in {member.value for member in UserRole}:
            raise SpecError(
                f"unknown role {self.role!r} for user {self.name!r}; "
                f"expected one of {', '.join(member.value for member in UserRole)}"
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
    #: First host port of this instance's allocated block; assigned at creation.
    port_base: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "users", tuple(self.users))
        object.__setattr__(self, "databases", tuple(self.databases))
        if not NAME_PATTERN.match(self.name):
            raise SpecError(
                f"instance name {self.name!r} must be lowercase letters, digits, '-' or '_', "
                "starting with a letter or digit, at most 31 characters"
            )
        if self.shards < 1 or self.replicas < 1:
            raise SpecError("shards and replicas must both be at least 1")
        if self.node_count > 30:
            raise SpecError(f"{self.node_count} nodes is more than chlab will launch at once")
        if self.keeper_nodes is not None and self.keeper_nodes < 0:
            raise SpecError("keeper_nodes cannot be negative")
        for database in self.databases:
            if database.engine == "Replicated" and self.effective_keeper_nodes == 0:
                raise SpecError(
                    f"database {database.name!r} uses the Replicated engine, which needs Keeper; "
                    "pass --keeper 1 or more"
                )

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

    def with_ports(self, port_base: int) -> "ClusterSpec":
        return replace(self, port_base=port_base)

    def with_version(self, version: str) -> "ClusterSpec":
        return replace(self, version=version)

    # --- serialization -------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClusterSpec":
        """Rebuild a spec, ignoring fields this version does not know about.

        Specs outlive the code that wrote them — an instance created by a different chlab
        must still be listable and removable rather than crashing the command.
        """
        payload = dict(data)
        payload["users"] = tuple(_construct(UserSpec, user) for user in payload.get("users") or ())
        payload["databases"] = tuple(
            _construct(DatabaseSpec, database) for database in payload.get("databases") or ()
        )
        return _construct(cls, payload)


def _construct(cls, data: dict[str, Any]):
    """Build a dataclass from a dict, dropping keys it does not declare."""
    known = set(cls.__dataclass_fields__)
    return cls(**{key: value for key, value in data.items() if key in known})


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
    """Parse a ``--user name[:password][:role]`` argument."""
    parts = text.split(":")
    if len(parts) > 3:
        raise SpecError(f"could not parse user {text!r}; expected name[:password][:role]")

    name = parts[0].strip()
    if not name:
        raise SpecError(f"user {text!r} has no name")

    password = parts[1] if len(parts) > 1 else ""
    role = (parts[2].strip().lower() or UserRole.READWRITE.value) if len(parts) > 2 else UserRole.READWRITE.value
    return UserSpec(name=name, password=password, role=role)


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
