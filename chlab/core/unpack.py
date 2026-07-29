"""Extraction of the ClickHouse binary from ``.deb`` and ``.tgz`` packages.

Implemented against the archive formats directly rather than shelling out to ``ar`` / ``dpkg``,
so chlab works on hosts without binutils or dpkg (macOS, minimal containers). Only the single
requested member is ever written out, so archive contents cannot escape the destination path.

Everything streams. An uncompressed ``clickhouse-common-static`` payload is comfortably over
2 GB and debug builds are larger, so nothing here may hold an archive — or a member — in
memory. Archives are read in a single pass, in ``tarfile``'s stream mode.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from .errors import ChlabError

AR_MAGIC = b"!<arch>\n"
AR_HEADER_SIZE = 60
AR_FILE_MAGIC = b"`\n"
COPY_BUFFER = 1 << 20


class UnpackError(ChlabError):
    pass


def extract_binary(package: Path, program: str, dest: Path) -> Path:
    """Extract ``usr/bin/<program>`` from a ``.deb`` or ``.tgz`` into ``dest``."""
    name = package.name.lower()
    if name.endswith(".deb"):
        with _deb_data_stream(package) as stream:
            return _extract_from_tar(stream, program, dest, origin=package.name)
    if name.endswith((".tgz", ".tar.gz")):
        with package.open("rb") as stream:
            return _extract_from_tar(stream, program, dest, origin=package.name)
    raise UnpackError(f"don't know how to unpack {package.name}")


class _BoundedReader(io.RawIOBase):
    """A read-only view of ``size`` bytes starting at the file object's current position."""

    def __init__(self, stream: BinaryIO, size: int):
        self._stream = stream
        self._remaining = size

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        if self._remaining <= 0:
            return 0
        chunk = self._stream.read(min(len(buffer), self._remaining))
        self._remaining -= len(chunk)
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _deb_data_stream(package: Path):
    """Open the ``data.tar.*`` member of a Debian package as a decompressed stream."""
    handle = package.open("rb")
    try:
        for name, size in _iter_ar_members(handle, package.name):
            if name.startswith("data.tar"):
                return _decompressed(name, _BoundedReader(handle, size), handle)
            handle.seek(size + (size % 2), io.SEEK_CUR)
    except BaseException:
        handle.close()
        raise
    handle.close()
    raise UnpackError(f"{package.name} contains no data.tar member")


def _iter_ar_members(handle: BinaryIO, origin: str) -> Iterator[tuple[str, int]]:
    """Yield ``(name, size)`` for each member of an ``ar`` archive, leaving the stream at its data."""
    if handle.read(len(AR_MAGIC)) != AR_MAGIC:
        raise UnpackError(f"{origin} is not a valid .deb (bad ar magic)")

    while header := handle.read(AR_HEADER_SIZE):
        if len(header) < AR_HEADER_SIZE:
            raise UnpackError(f"{origin} has a truncated ar header")
        if header[58:60] != AR_FILE_MAGIC:
            raise UnpackError(f"{origin} has a corrupt ar header")

        raw_name = header[0:16].decode("ascii", "replace").strip()
        if raw_name.startswith("//") or raw_name.startswith("#1/") or raw_name.startswith("/"):
            # GNU and BSD extended-filename tables. dpkg never emits them, so rather than
            # implement the formats, say so plainly instead of blaming the header.
            raise UnpackError(f"{origin} uses extended ar filenames, which chlab does not support")

        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError as error:
            raise UnpackError(f"{origin} has an unreadable ar member size") from error

        yield raw_name.rstrip("/"), size


def _decompressed(member_name: str, stream, owner):
    """Wrap a ``data.tar.*`` stream in the right decompressor.

    ``tarfile`` handles gzip, xz and bzip2 natively in stream mode; zstd (used by newer dpkg)
    does not, so it is decompressed on the fly by the ``zstandard`` package or the ``zstd`` CLI.
    """
    if not member_name.endswith(".zst"):
        return _closing(stream, owner)

    try:
        import zstandard
    except ImportError:
        pass
    else:
        return _closing(zstandard.ZstdDecompressor().stream_reader(stream), owner)

    zstd = shutil.which("zstd") or shutil.which("unzstd")
    if zstd is None:
        raise UnpackError(
            "package uses zstd compression; install the 'zstandard' Python package "
            "or the 'zstd' command-line tool"
        )
    process = subprocess.Popen([zstd, "-d", "-c"], stdin=stream, stdout=subprocess.PIPE)
    return _closing(process.stdout, owner, process)


class _closing:
    """A context manager that closes the wrapped stream and everything behind it."""

    def __init__(self, stream, *owners):
        self.stream = stream
        self._owners = owners

    def __enter__(self):
        return self.stream

    def __exit__(self, *exc_info) -> None:
        for closeable in (self.stream, *self._owners):
            try:
                closeable.close()
            except Exception:
                pass


def _extract_from_tar(stream, program: str, dest: Path, *, origin: str) -> Path:
    """Find ``usr/bin/<program>`` in a tar stream and write it to ``dest``.

    Stream mode means one pass and no random access, so the member is written the moment it is
    seen rather than located first and extracted second.
    """
    wanted = f"usr/bin/{program}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    partial = dest.with_name(dest.name + ".part")

    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            for member in archive:
                if not member.isfile() or not _matches(member.name, wanted):
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise UnpackError(f"{wanted} in {origin} is not a regular file")
                with partial.open("wb") as handle:
                    shutil.copyfileobj(extracted, handle, COPY_BUFFER)
                partial.chmod(0o755)
                partial.replace(dest)
                return dest
    except tarfile.TarError as error:
        partial.unlink(missing_ok=True)
        raise UnpackError(f"could not read {origin}: {error}") from error
    except BaseException:
        partial.unlink(missing_ok=True)
        raise

    raise UnpackError(f"{origin} does not contain {wanted}")


def _matches(name: str, wanted: str) -> bool:
    """Match ``usr/bin/<program>``, tolerating ``./`` and version-prefixed layouts.

    Debian payloads use ``./usr/bin/clickhouse``; tgz builds nest everything under a
    ``clickhouse-common-static-<version>/`` directory. The path suffix is always required — a
    bare basename match could pick up a debug-symbols copy from elsewhere in the archive.
    """
    normalized = name.lstrip("./")
    return normalized == wanted or normalized.endswith("/" + wanted)
