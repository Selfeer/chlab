"""Saved topologies.

A profile is a ClusterSpec with the instance-specific parts removed — no name, no source, no
ports — so the same shape can be relaunched against any binary:

    chlab up --clickhouse <any source> --profile 3x2
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .spec import ClusterSpec, DatabaseSpec, UserSpec

ENV_CONFIG_DIR = "CHLAB_CONFIG_DIR"
#: Fields a profile never carries, because they identify a particular instance.
INSTANCE_FIELDS = frozenset({"name", "source", "version", "port_base"})


class ProfileNotFound(KeyError):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"no profile named {self.name!r}"


def default_config_dir() -> Path:
    override = os.environ.get(ENV_CONFIG_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "chlab"


@dataclass
class Profiles:
    root: Path

    @classmethod
    def default(cls) -> "Profiles":
        return cls(default_config_dir())

    @property
    def path(self) -> Path:
        return self.root / "profiles.json"

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.part")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        tmp.replace(self.path)

    def names(self) -> list[str]:
        return sorted(self._read())

    def all(self) -> dict[str, dict[str, Any]]:
        return self._read()

    def get(self, name: str) -> dict[str, Any]:
        data = self._read()
        if name not in data:
            raise ProfileNotFound(name)
        return data[name]

    def save(self, name: str, spec: ClusterSpec) -> None:
        data = self._read()
        data[name] = {
            key: value for key, value in spec.to_dict().items() if key not in INSTANCE_FIELDS
        }
        self._write(data)

    def delete(self, name: str) -> None:
        data = self._read()
        if name not in data:
            raise ProfileNotFound(name)
        del data[name]
        self._write(data)


def apply(profile: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge a profile under explicit overrides, which always win."""
    merged = dict(profile)
    merged.update({key: value for key, value in overrides.items() if value is not None})
    if "users" in merged:
        merged["users"] = tuple(
            user if isinstance(user, UserSpec) else UserSpec(**user) for user in merged["users"]
        )
    if "databases" in merged:
        merged["databases"] = tuple(
            database if isinstance(database, DatabaseSpec) else DatabaseSpec(**database)
            for database in merged["databases"]
        )
    return merged


def summarise(profile: dict[str, Any]) -> str:
    """A one-line description for listings."""
    shards = profile.get("shards", 1)
    replicas = profile.get("replicas", 1)
    parts = [f"{shards} shard(s) x {replicas} replica(s)"]
    keeper = profile.get("keeper_nodes")
    if keeper:
        parts.append(f"{keeper} keeper")
    if profile.get("users"):
        parts.append(f"{len(profile['users'])} user(s)")
    if profile.get("databases"):
        parts.append(f"{len(profile['databases'])} database(s)")
    if profile.get("seed"):
        parts.append("seeded")
    return ", ".join(parts)
