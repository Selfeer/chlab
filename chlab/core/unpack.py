"""Extraction of the ClickHouse binary from ``.deb`` and ``.tgz`` packages.

Implemented against the archive formats directly rather than shelling out to ``ar`` / ``dpkg``,
so chlab works on hosts without binutils or dpkg (macOS, minimal containers). Only the single
requested member is ever written out, so archive contents cannot escape the destination path.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

AR_MAGIC = b"!<arch>\n"
AR_HEADER_SIZE = 60
AR_FILE_MAGIC = b"`\n"


class UnpackError(RuntimeError):
    pass


def extract_binary(package: Path, program: str, dest: Path) -> Path:
    """Extract ``usr/bin/<program>`` from a ``.deb`` or ``.tgz`` into ``dest``."""
    name = package.name.lower()
    if name.endswith(".deb"):
        payload = _deb_data_tar(package)
    elif name.endswith((".tgz", ".tar.gz")):
        payload = package.read_bytes()
    else:
        raise UnpackError(f"don't know how to unpack {package.name}")

    return _extract_from_tar(payload, program, dest, origin=package.name)


def _deb_data_tar(package: Path) -> bytes:
    """Return the decompressed ``data.tar.*`` payload of a Debian package."""
    for member_name, member_data in _iter_ar_members(package):
        if member_name.startswith("data.tar"):
            return _decompress(member_name, member_data)
    raise UnpackError(f"{package.name} contains no data.tar member")


def _iter_ar_members(package: Path):
    """Yield ``(name, data)`` for each member of an ``ar`` archive."""
    with package.open("rb") as handle:
        if handle.read(len(AR_MAGIC)) != AR_MAGIC:
            raise UnpackError(f"{package.name} is not a valid .deb (bad ar magic)")

        while header := handle.read(AR_HEADER_SIZE):
            if len(header) < AR_HEADER_SIZE:
                raise UnpackError(f"{package.name} has a truncated ar header")
            if header[58:60] != AR_FILE_MAGIC:
                raise UnpackError(f"{package.name} has a corrupt ar header")

            name = header[0:16].decode("ascii", "replace").strip().rstrip("/")
            try:
                size = int(header[48:58].decode("ascii").strip())
            except ValueError as error:
                raise UnpackError(f"{package.name} has an unreadable ar member size") from error

            data = handle.read(size)
            if len(data) < size:
                raise UnpackError(f"{package.name} is truncated inside member {name!r}")
            if size % 2:
                handle.read(1)  # members are padded to an even offset

            yield name, data


def _decompress(member_name: str, data: bytes) -> bytes:
    """Decompress a ``data.tar.*`` member.

    ``tarfile`` handles gzip, xz and bzip2 natively; zstd (used by newer dpkg) does not, so it
    is decompressed up front via the ``zstandard`` package or the ``zstd`` CLI.
    """
    if not member_name.endswith(".zst"):
        return data

    try:
        import zstandard
    except ImportError:
        pass
    else:
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=0)

    zstd = shutil.which("zstd") or shutil.which("unzstd")
    if zstd is None:
        raise UnpackError(
            "package uses zstd compression; install the 'zstandard' Python package "
            "or the 'zstd' command-line tool"
        )
    result = subprocess.run(
        [zstd, "-d", "-c"], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    if result.returncode != 0:
        raise UnpackError(f"zstd decompression failed: {result.stderr.decode(errors='replace')}")
    return result.stdout


def _extract_from_tar(payload: bytes, program: str, dest: Path, *, origin: str) -> Path:
    wanted = f"usr/bin/{program}"
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:*") as archive:
        member = _find_member(archive, wanted, program)
        if member is None:
            raise UnpackError(f"{origin} does not contain {wanted}")
        extracted = archive.extractfile(member)
        if extracted is None:
            raise UnpackError(f"{wanted} in {origin} is not a regular file")

        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        try:
            with partial.open("wb") as handle:
                shutil.copyfileobj(extracted, handle)
            partial.chmod(0o755)
            partial.replace(dest)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise

    return dest


def _find_member(archive: tarfile.TarFile, wanted: str, program: str):
    """Locate ``usr/bin/<program>``, tolerating ``./`` and version-prefixed layouts.

    Debian payloads use ``./usr/bin/clickhouse``; tgz builds nest everything under a
    ``clickhouse-common-static-<version>/`` directory.
    """
    fallback = None
    for member in archive.getmembers():
        if not member.isfile():
            continue
        normalized = member.name.lstrip("./")
        if normalized == wanted or normalized.endswith("/" + wanted):
            return member
        if fallback is None and normalized.rsplit("/", 1)[-1] == program:
            fallback = member
    return fallback
