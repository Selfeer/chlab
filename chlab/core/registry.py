"""Where instances live on disk.

    $XDG_STATE_HOME/chlab/instances/<name>/
      spec.json
      docker-compose.yml
      configs/<node>/...

There is no separate index file: the directory listing *is* the registry, so a half-written
index can never disagree with what actually exists.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import confine
from .errors import ChlabError
from .spec import ClusterSpec
from .topology import port_span

ENV_STATE_DIR = "CHLAB_STATE_DIR"
SPEC_FILENAME = "spec.json"
#: spec.json records user passwords in cleartext, so it is not world-readable.
SPEC_MODE = 0o600
STATE_DIR_MODE = 0o700


class InstanceNotFound(ChlabError):
    def __init__(self, name: str):
        super().__init__(name)
        self.name = name

    def __str__(self) -> str:
        return f"no instance named {self.name!r}"


class InstanceExists(ChlabError):
    pass


class CorruptSpec(ChlabError):
    pass


def default_state_dir() -> Path:
    override = os.environ.get(ENV_STATE_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "chlab"


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = root or default_state_dir()

    @property
    def instances_dir(self) -> Path:
        return self.root / "instances"

    def instance_dir(self, name: str) -> Path:
        # Every lookup by name comes through here, so this is where a reproduction step's
        # child CLI is held to its own instance.
        confine.check_instance(name)
        return self.instances_dir / name

    def exists(self, name: str) -> bool:
        return (self.instance_dir(name) / SPEC_FILENAME).is_file()

    def save(self, spec: ClusterSpec) -> Path:
        directory = self.instance_dir(spec.name)
        directory.mkdir(parents=True, exist_ok=True)
        _restrict(self.root)
        path = directory / SPEC_FILENAME
        tmp = path.with_suffix(".json.part")
        tmp.write_text(json.dumps(spec.to_dict(), indent=2, sort_keys=True) + "\n")
        tmp.chmod(SPEC_MODE)
        tmp.replace(path)
        return directory

    def load(self, name: str) -> ClusterSpec:
        path = self.instance_dir(name) / SPEC_FILENAME
        try:
            return ClusterSpec.from_dict(json.loads(path.read_text()))
        except FileNotFoundError as error:
            raise InstanceNotFound(name) from error
        except json.JSONDecodeError as error:
            # Without this the instance is listable but not removable: list() skips it while
            # status/stop/rm all traceback.
            raise CorruptSpec(
                f"{path} is not readable JSON ({error}); "
                f"delete the directory to forget this instance: {self.instance_dir(name)}"
            ) from error

    def list(self) -> list[ClusterSpec]:
        if not self.instances_dir.is_dir():
            return []
        specs = []
        for directory in sorted(self.instances_dir.iterdir()):
            if not confine.instance_allowed(directory.name):
                continue
            try:
                specs.append(self.load(directory.name))
            except (ChlabError, json.JSONDecodeError, TypeError, ValueError):
                continue
        return specs

    def delete(self, name: str) -> None:
        directory = self.instance_dir(name)
        if not directory.is_dir():
            raise InstanceNotFound(name)
        shutil.rmtree(directory)

    def taken_ports(self, *, excluding: str | None = None) -> set[int]:
        """Every port promised to an existing instance, running or not."""
        taken: set[int] = set()
        for spec in self.list():
            if spec.name == excluding or spec.port_base is None:
                continue
            taken.update(range(spec.port_base, spec.port_base + port_span(spec)))
        return taken


def _restrict(path: Path) -> None:
    """Keep the state tree private; it holds cleartext passwords."""
    try:
        path.chmod(STATE_DIR_MODE)
    except OSError:
        pass
