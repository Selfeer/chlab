"""Streaming HTTP download.

Uses the standard library so chlab has no hard dependency on an HTTP client. Downloads land in
a ``.part`` file and are renamed into place only on success, so an interrupted transfer never
looks like a complete artifact.
"""

from __future__ import annotations

import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

CHUNK_SIZE = 1 << 20
USER_AGENT = "chlab"

#: ``(downloaded_bytes, total_bytes_or_None)``, called repeatedly during transfer.
ProgressCallback = Callable[[int, int | None], None]


class DownloadError(RuntimeError):
    pass


def download(
    url: str,
    dest: Path,
    *,
    force: bool = False,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download ``url`` to ``dest``, returning ``dest``.

    An existing, complete ``dest`` is reused unless ``force`` is set.
    """
    if dest.exists() and not force:
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(request) as response:
            total = _content_length(response)
            downloaded = 0
            if on_progress:
                on_progress(0, total)
            with partial.open("wb") as handle:
                while chunk := response.read(CHUNK_SIZE):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if on_progress:
                        on_progress(downloaded, total)
    except urllib.error.HTTPError as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"{url} returned HTTP {error.code} {error.reason}") from error
    except urllib.error.URLError as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"could not fetch {url}: {error.reason}") from error
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    partial.replace(dest)
    return dest


def copy_local(src: Path, dest: Path) -> Path:
    """Copy a local artifact into the cache, atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    try:
        shutil.copyfile(src, partial)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    partial.replace(dest)
    return dest


def _content_length(response) -> int | None:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
