"""Turn a source string into something runnable: a docker image, or a binary on disk."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .cache import Cache, cache_key
from .download import ProgressCallback, copy_local, download
from .sources import ArtifactType, Source, SourceKind, parse_source, version_from_image_tag
from .unpack import extract_binary

DEFAULT_PROGRAM = "clickhouse"
#: Throwaway image used to run a static binary when the host cannot execute it directly.
VERSION_PROBE_IMAGE = "ubuntu:24.04"
VERSION_TIMEOUT = 120
PULL_TIMEOUT = 1800

EventCallback = Callable[[str], None]

_VERSION = re.compile(r"version\s+([0-9][0-9A-Za-z.\-+_]*)", re.IGNORECASE)


class ResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class Resolved:
    """A source that is now ready to run."""

    source: Source
    version: str | None
    docker_image: str | None = None
    binary_path: Path | None = None
    #: True when nothing had to be downloaded, unpacked or pulled.
    cached: bool = False

    @property
    def runs_as_image(self) -> bool:
        """Docker sources run as their own image; everything else runs on the base image."""
        return self.docker_image is not None


def resolve(
    raw: str,
    *,
    program: str = DEFAULT_PROGRAM,
    version: str | None = None,
    force: bool = False,
    cache: Cache | None = None,
    on_event: EventCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> Resolved:
    """Resolve ``raw`` into a runnable image or binary.

    ``version`` overrides detection entirely, mirroring ``--clickhouse-version`` in Altinity's
    regression suite. ``force`` re-downloads and re-detects, ignoring the cache.
    """
    source = parse_source(raw)
    cache = cache or Cache.default()
    key = cache_key(source)
    notify = on_event or (lambda _message: None)

    if source.is_docker:
        return _resolve_docker(source, key, cache, program, version, force, notify)
    return _resolve_file(source, key, cache, program, version, force, notify, on_progress)


def _resolve_docker(
    source: Source,
    key: str,
    cache: Cache,
    program: str,
    version: str | None,
    force: bool,
    notify: EventCallback,
) -> Resolved:
    image = source.value
    already_present = _image_present(image)

    if force or not already_present:
        notify(f"pulling {image}")
        _pull_image(image)

    detected = version or _cached_version(cache, key, program, force)
    if detected is None:
        detected = _version_from_image(image, program)
        if detected is None:
            detected = version_from_image_tag(image)
            if detected is None:
                raise ResolveError(
                    f"could not determine the ClickHouse version of {image}; "
                    "pass --clickhouse-version to set it explicitly"
                )
            notify(
                f"could not run {program} inside {image}; "
                f"falling back to the version in its tag ({detected})"
            )

    _write_meta(cache, key, source, program, detected, docker_image=image)
    return Resolved(
        source=source,
        version=detected,
        docker_image=image,
        cached=already_present and not force,
    )


def _resolve_file(
    source: Source,
    key: str,
    cache: Cache,
    program: str,
    version: str | None,
    force: bool,
    notify: EventCallback,
    on_progress: ProgressCallback | None,
) -> Resolved:
    if source.kind is SourceKind.PATH and source.artifact is ArtifactType.BINARY:
        # A local binary is used where it lies — no point copying gigabytes into the cache,
        # and it keeps `--clickhouse ./build/clickhouse` tracking a live build tree.
        binary_path = _local_binary(source)
        cached = cache.read_meta(key) is not None and not force
    else:
        binary_path = cache.bin_dir(key) / program
        cached = binary_path.exists() and not force
        if not cached:
            package = _fetch_artifact(source, key, cache, force, notify, on_progress)
            if source.artifact is ArtifactType.BINARY:
                binary_path = _install_binary(package, binary_path)
            else:
                notify(f"unpacking {package.name}")
                binary_path = extract_binary(package, program, binary_path)

    detected = version or _cached_version(cache, key, program, force)
    if detected is None:
        detected = _version_from_binary(binary_path, program)
        if detected is None:
            raise ResolveError(
                f"could not determine the ClickHouse version of {binary_path}; "
                "pass --clickhouse-version to set it explicitly"
            )

    _write_meta(cache, key, source, program, detected, binary_path=binary_path)
    return Resolved(source=source, version=detected, binary_path=binary_path, cached=cached)


def _fetch_artifact(
    source: Source,
    key: str,
    cache: Cache,
    force: bool,
    notify: EventCallback,
    on_progress: ProgressCallback | None,
) -> Path:
    """Get the referenced file onto local disk and return its path."""
    if source.kind is SourceKind.URL:
        filename = source.filename or DEFAULT_PROGRAM
        dest = cache.artifact_dir(key) / filename
        if not dest.exists() or force:
            notify(f"downloading {filename}")
        return download(source.value, dest, force=force, on_progress=on_progress)

    return _existing_file(Path(source.value))


def _local_binary(source: Source) -> Path:
    path = _existing_file(Path(source.value))
    _make_executable(path)
    return path


def _existing_file(path: Path) -> Path:
    path = path.expanduser()
    if not path.exists():
        raise ResolveError(f"{path} does not exist")
    if not path.is_file():
        raise ResolveError(f"{path} is not a file")
    return path.resolve()


def _install_binary(package: Path, dest: Path) -> Path:
    """Copy a raw (already-unpacked) binary into the cache."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    copy_local(package, dest)
    dest.chmod(0o755)
    return dest


def _make_executable(path: Path) -> None:
    try:
        path.chmod(path.stat().st_mode | 0o111)
    except OSError:
        # Read-only or not ours: the runtime mounts it read-only anyway.
        pass


# --- version detection -------------------------------------------------------------------


def _version_from_binary(binary: Path, program: str) -> str | None:
    """Ask the binary for its version, on the host if possible and in a container if not."""
    on_host = _run([str(binary), "server", "--version"], timeout=VERSION_TIMEOUT)
    if on_host is not None:
        parsed = _parse_version(on_host)
        if parsed:
            return parsed

    if not _docker_available():
        return None

    mount = f"{binary.resolve()}:/{program}:ro"
    in_container = _run(
        [
            "docker", "run", "--rm", "--pull", "missing",
            "-v", mount, "--entrypoint", f"/{program}",
            VERSION_PROBE_IMAGE, "server", "--version",
        ],
        timeout=VERSION_TIMEOUT,
    )
    return _parse_version(in_container) if in_container else None


def _version_from_image(image: str, program: str) -> str | None:
    output = _run(
        ["docker", "run", "--rm", "--entrypoint", f"/usr/bin/{program}", image, "server", "--version"],
        timeout=VERSION_TIMEOUT,
    )
    return _parse_version(output) if output else None


def _parse_version(output: str) -> str | None:
    match = _VERSION.search(output)
    if not match:
        return None
    return match.group(1).strip(".")


def _cached_version(cache: Cache, key: str, program: str, force: bool) -> str | None:
    if force:
        return None
    meta = cache.read_meta(key)
    if not meta or meta.get("program") != program:
        return None
    return meta.get("version")


def _write_meta(
    cache: Cache,
    key: str,
    source: Source,
    program: str,
    version: str | None,
    *,
    docker_image: str | None = None,
    binary_path: Path | None = None,
) -> None:
    cache.write_meta(
        key,
        {
            "source": source.raw,
            "kind": source.kind.value,
            "program": program,
            "version": version,
            "docker_image": docker_image,
            "binary_path": str(binary_path) if binary_path else None,
            "resolved_at": int(time.time()),
        },
    )


# --- docker helpers ----------------------------------------------------------------------


def _docker_available() -> bool:
    return shutil.which("docker") is not None


def _require_docker() -> None:
    if not _docker_available():
        raise ResolveError("docker is required for docker:// sources but was not found on PATH")


def _image_present(image: str) -> bool:
    if not _docker_available():
        return False
    return _run(["docker", "image", "inspect", image]) is not None


def _pull_image(image: str) -> None:
    _require_docker()
    result = subprocess.run(
        ["docker", "pull", image],
        capture_output=True,
        text=True,
        timeout=PULL_TIMEOUT,
    )
    if result.returncode != 0:
        raise ResolveError(f"could not pull {image}: {result.stderr.strip() or result.stdout.strip()}")


def _run(command: list[str], *, timeout: int = 60) -> str | None:
    """Run a command, returning its combined output, or None if it could not succeed."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return f"{result.stdout}\n{result.stderr}"
