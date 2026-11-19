"""Streaming HTTP download.

Uses the standard library so chlab has no hard dependency on an HTTP client. Downloads land in
a ``.part`` file and are renamed into place only on success, so an interrupted transfer never
looks like a complete artifact.
"""

from __future__ import annotations

import hashlib
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

from .errors import ChlabError

CHUNK_SIZE = 1 << 20
USER_AGENT = "chlab"
#: Seconds a connection may sit silent — connecting, or between two reads — before the
#: download is abandoned. Not a limit on the whole transfer: a debug build is gigabytes.
READ_TIMEOUT = 120

#: ``(downloaded_bytes, total_bytes_or_None)``, called repeatedly during transfer.
ProgressCallback = Callable[[int, int | None], None]


class DownloadError(ChlabError):
    pass


class ChecksumError(ChlabError):
    pass


class _NoDowngrade(urllib.request.HTTPRedirectHandler):
    """Follow redirects, except from https:// to plain http://.

    urllib follows a downgrade silently, which would undo the transport protection the user
    asked for by typing https:// — and the plain-HTTP warning only sees the first URL.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if req.full_url.startswith("https://") and not newurl.startswith("https://"):
            raise DownloadError(f"{req.full_url} redirects to {newurl}, which is not https://")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_NoDowngrade)


def download(
    url: str,
    dest: Path,
    *,
    force: bool = False,
    sha256: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> Path:
    """Download ``url`` to ``dest``, returning ``dest``.

    An existing, complete ``dest`` is reused unless ``force`` is set. If ``sha256`` is given,
    the finished file must match it.
    """
    if dest.exists() and not force:
        if sha256:
            verify_sha256(dest, sha256)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    try:
        with _opener.open(request, timeout=READ_TIMEOUT) as response:
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

        # http.client does not raise when a body ends short of its Content-Length, so a
        # connection dropped mid-transfer looks exactly like a completed one. Without this
        # check the truncated file is renamed into place and cached forever.
        if total is not None and downloaded != total:
            raise DownloadError(
                f"{url} ended after {downloaded} of {total} bytes; the transfer was cut short"
            )
    except urllib.error.HTTPError as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"{url} returned HTTP {error.code} {error.reason}") from error
    except urllib.error.URLError as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(f"could not fetch {url}: {error.reason}") from error
    except TimeoutError as error:
        partial.unlink(missing_ok=True)
        raise DownloadError(
            f"{url} sent nothing for {READ_TIMEOUT}s; the transfer was abandoned"
        ) from error
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    if sha256:
        try:
            verify_sha256(partial, sha256)
        except ChecksumError:
            partial.unlink(missing_ok=True)
            raise

    partial.replace(dest)
    return dest


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: Path, expected: str) -> None:
    actual = sha256_of(path)
    if actual.lower() != expected.strip().lower():
        raise ChecksumError(f"{path.name} has sha256 {actual}, expected {expected}")


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
