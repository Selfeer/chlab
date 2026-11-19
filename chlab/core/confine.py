"""What a reproduction's ``.chlab`` step may touch.

A ``.chlab`` step runs chlab itself, on the host, and chlab can read and write host files
(``cp``, ``s3 get``, ``query -f``, …) and act on any instance. A reproduction directory is
often written by an agent from a PR's own text, so the runner puts its child CLI in a
confinement given by two environment variables:

* ``CHLAB_CONFINE_INSTANCE`` — the only instance the step may name. Any other behaves as if
  it did not exist.
* ``CHLAB_CONFINE_DIR`` — the only host directory the step may read from or write to: the
  reproduction's own. Paths are resolved, symlinks included, before the check.

Everything else a reproduction runs — SQL, ``.sh``, ``.py``, Spark — already runs inside
the instance's containers, which see only the reproduction directory.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import ChlabError

ENV_INSTANCE = "CHLAB_CONFINE_INSTANCE"
ENV_DIRECTORY = "CHLAB_CONFINE_DIR"


class ConfinementError(ChlabError):
    pass


def environment(instance: str, directory: Path) -> dict[str, str]:
    return {ENV_INSTANCE: instance, ENV_DIRECTORY: str(directory.resolve())}


def instance_allowed(name: str) -> bool:
    only = os.environ.get(ENV_INSTANCE)
    return not only or name == only


def check_instance(name: str) -> None:
    if not instance_allowed(name):
        raise ConfinementError(
            f"a reproduction step may use only its own instance, not {name!r}; "
            "write {instance} where the step needs its name"
        )


def local_path(text: str | Path) -> Path:
    """``text`` as an absolute host path, refused if a confined step reaches outside."""
    path = Path(text).expanduser().resolve()
    root = os.environ.get(ENV_DIRECTORY)
    if root and not path.is_relative_to(Path(root)):
        raise ConfinementError(
            f"{text} is outside the reproduction directory; a step may read and write only "
            "files inside it"
        )
    return path
