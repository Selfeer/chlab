"""On-disk cache layout.

    $XDG_CACHE_HOME/chlab/            (default ~/.cache/chlab)
      artifacts/<key>/<filename>      downloaded .deb / .tgz / raw binary
      bin/<key>/clickhouse            extracted, executable
      bin/<key>/meta.json             {source, version, resolved_at}

The key is derived from the source string. For local paths the file's size and mtime are mixed
in, so rebuilding a binary in place invalidates its entry instead of serving a stale version.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from .sources import Source, SourceKind

KEY_LENGTH = 16
ENV_CACHE_DIR = "CHLAB_CACHE_DIR"


def default_cache_dir() -> Path:
    override = os.environ.get(ENV_CACHE_DIR)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "chlab"


def cache_key(source: Source) -> str:
    """Stable identifier for a source, sensitive to local file mutation."""
    parts = [source.kind.value, source.value]
    if source.kind is SourceKind.PATH:
        try:
            stat = Path(source.value).stat()
            parts += [str(stat.st_size), str(int(stat.st_mtime))]
        except OSError:
            # Missing file: resolution will fail with a better message than we can give here.
            pass
    digest = hashlib.sha256("\0".join(parts).encode()).hexdigest()
    return digest[:KEY_LENGTH]


@dataclass(frozen=True)
class Cache:
    root: Path

    @classmethod
    def default(cls) -> "Cache":
        return cls(default_cache_dir())

    def artifact_dir(self, key: str) -> Path:
        return self.root / "artifacts" / key

    def bin_dir(self, key: str) -> Path:
        return self.root / "bin" / key

    def meta_path(self, key: str) -> Path:
        return self.bin_dir(key) / "meta.json"

    def read_meta(self, key: str) -> dict | None:
        path = self.meta_path(key)
        try:
            with path.open() as handle:
                return json.load(handle)
        except (OSError, json.JSONDecodeError):
            return None

    def write_meta(self, key: str, meta: dict) -> None:
        path = self.meta_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.part")
        with tmp.open("w") as handle:
            json.dump(meta, handle, indent=2, sort_keys=True)
            handle.write("\n")
        tmp.replace(path)
