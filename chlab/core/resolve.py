"""Turn a source string into something runnable: a docker image, or a binary on disk."""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .cache import Cache, cache_key
from .download import ChecksumError, ProgressCallback, copy_local, download, verify_sha256
from .errors import ChlabError
from .sources import ArtifactType, Source, SourceKind, parse_source, version_from_image_tag
from .unpack import extract_binary

#: chlab only ever extracts the ClickHouse binary; Keeper runs from the same one.
PROGRAM = "clickhouse"
#: Throwaway image used to run a static binary when the host cannot execute it directly.
VERSION_PROBE_IMAGE = "ubuntu:24.04"
VERSION_TIMEOUT = 120
PULL_TIMEOUT = 1800

EventCallback = Callable[[str], None]

_VERSION = re.compile(r"version\s+([0-9][0-9A-Za-z.\-+_]*)", re.IGNORECASE)


class ResolveError(ChlabError):
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
    version: str | None = None,
    force: bool = False,
    sha256: str | None = None,
    trust_local: bool = True,
    cache: Cache | None = None,
    on_event: EventCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> Resolved:
    """Resolve ``raw`` into a runnable image or binary.

    ``version`` overrides detection entirely, mirroring ``--clickhouse-version`` in Altinity's
    regression suite. ``force`` re-downloads and re-detects, ignoring the cache. ``sha256``,
    when given, must match the artifact — downloaded, cached or local.

    ``trust_local`` says whether a local path came from the user. Set it to False for a path
    named by a file someone else wrote, such as a reproduction's ``repro.yaml``: the binary is
    then only ever executed inside a container, as a downloaded one is.
    """
    source = parse_source(raw)
    cache = cache or Cache.default()
    key = cache_key(source)
    notify = on_event or (lambda _message: None)

    if source.kind is SourceKind.URL and source.value.startswith("http://"):
        notify(
            "warning: this source is plain HTTP, so its contents can be replaced in transit; "
            "prefer https:// or pass --sha256"
        )

    if source.is_docker:
        return _resolve_docker(source, key, cache, version, force, notify)
    trusted = trust_local and source.kind is SourceKind.PATH
    return _resolve_file(source, key, cache, version, force, sha256, trusted, notify, on_progress)


def _resolve_docker(
    source: Source,
    key: str,
    cache: Cache,
    version: str | None,
    force: bool,
    notify: EventCallback,
) -> Resolved:
    image = source.value
    already_present = _image_present(image)

    if already_present and not force and _is_floating_tag(image):
        notify(
            f"warning: {image} has a moving tag, so the local copy may be stale; "
            "pass --force-resolve to re-pull"
        )

    if force or not already_present:
        notify(f"pulling {image}")
        _pull_image(image)

    detected = version or _cached_version(cache, key, force)
    if detected is None:
        detected = _version_from_image(image)
        if detected is None:
            detected = version_from_image_tag(image)
            if detected is None:
                raise ResolveError(
                    f"could not determine the ClickHouse version of {image}; "
                    "pass --clickhouse-version to set it explicitly"
                )
            notify(
                f"could not run {PROGRAM} inside {image}; "
                f"falling back to the version in its tag ({detected})"
            )

    _write_meta(cache, key, source, detected, docker_image=image)
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
    version: str | None,
    force: bool,
    sha256: str | None,
    trusted: bool,
    notify: EventCallback,
    on_progress: ProgressCallback | None,
) -> Resolved:
    meta = cache.read_meta(key) or {}
    expected = sha256.strip().lower() if sha256 else None
    if source.kind is SourceKind.PATH and source.artifact is ArtifactType.BINARY:
        # A local binary is used where it lies — no point copying gigabytes into the cache,
        # and it keeps `--clickhouse ./build/clickhouse` tracking a live build tree.
        binary_path = _local_binary(source)
        if expected:
            verify_sha256(binary_path, expected)
        cached = bool(meta) and not force
    else:
        binary_path = cache.bin_dir(key) / PROGRAM
        cached = binary_path.exists() and not force
        if cached and expected and meta.get("artifact_sha256") != expected:
            # The extracted binary came from an artifact nobody checked against this digest:
            # fetch (or re-verify) the artifact and extract again rather than trust it.
            cached = False
        if not cached:
            package = _fetch_artifact(source, key, cache, force, expected, notify, on_progress)
            if source.artifact is ArtifactType.BINARY:
                binary_path = _install_binary(package, binary_path)
            else:
                notify(f"unpacking {package.name}")
                binary_path = extract_binary(package, PROGRAM, binary_path)
            # A fresh binary may not be the one the cached version was read from.
            force = True

    detected = version or _cached_version(cache, key, force)
    if detected is None:
        detected = _version_from_binary(binary_path, trusted=trusted)
        if detected is None:
            raise ResolveError(
                f"could not determine the ClickHouse version of {binary_path}; "
                "pass --clickhouse-version to set it explicitly"
            )

    _write_meta(
        cache,
        key,
        source,
        detected,
        binary_path=binary_path,
        artifact_sha256=expected or (meta.get("artifact_sha256") if cached else None),
    )
    return Resolved(source=source, version=detected, binary_path=binary_path, cached=cached)


def _fetch_artifact(
    source: Source,
    key: str,
    cache: Cache,
    force: bool,
    sha256: str | None,
    notify: EventCallback,
    on_progress: ProgressCallback | None,
) -> Path:
    """Get the referenced file onto local disk and return its path."""
    if source.kind is SourceKind.URL:
        filename = source.filename or PROGRAM
        dest = cache.artifact_dir(key) / filename
        if dest.exists() and not force and sha256:
            try:
                verify_sha256(dest, sha256)
            except ChecksumError:
                # A cached download that does not match is replaced, not trusted.
                notify(f"cached {filename} does not match --sha256; downloading it again")
                force = True
        if not dest.exists() or force:
            notify(f"downloading {filename}")
        return download(source.value, dest, force=force, sha256=sha256, on_progress=on_progress)

    path = _existing_file(Path(source.value))
    if sha256:
        verify_sha256(path, sha256)
    return path


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


def _version_from_binary(binary: Path, *, trusted: bool) -> str | None:
    """Ask the binary for its version.

    Determining the version means executing the artifact, so where that happens matters. A
    binary chlab did not get from the user directly — downloaded from a URL, or named by a
    reproduction's repro.yaml — runs **only in a container**, with no network. Running it on
    the host, even as a fallback, would let a hostile artifact fail the container probe on
    purpose and get executed outside Docker, so there is no fallback: the version must then
    be given with ``--clickhouse-version``.

    A binary the user named on the command line (``trusted``) is run on the host first
    instead: they chose that file, and requiring Docker to read a local build's version
    would break working offline. The result is cached either way.
    """
    attempts = [_host_probe, _container_probe] if trusted else [_container_probe]
    for attempt in attempts:
        output = attempt(binary)
        if output:
            parsed = _parse_version(output)
            if parsed:
                return parsed
    return None


def _host_probe(binary: Path) -> str | None:
    return _run([str(binary), "server", "--version"], timeout=VERSION_TIMEOUT)


def _container_probe(binary: Path) -> str | None:
    if not _docker_available():
        return None
    return _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--pull",
            "missing",
            "-v",
            f"{binary.resolve()}:/{PROGRAM}:ro",
            "--entrypoint",
            f"/{PROGRAM}",
            VERSION_PROBE_IMAGE,
            "server",
            "--version",
        ],
        timeout=VERSION_TIMEOUT,
    )


def _version_from_image(image: str) -> str | None:
    output = _run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            f"/usr/bin/{PROGRAM}",
            image,
            "server",
            "--version",
        ],
        timeout=VERSION_TIMEOUT,
    )
    return _parse_version(output) if output else None


def _parse_version(output: str) -> str | None:
    match = _VERSION.search(output)
    if not match:
        return None
    return match.group(1).strip(".")


def _is_floating_tag(image: str) -> bool:
    """True for tags that move, where a cached local image may not be what the registry serves."""
    _, _, tag = image.rpartition(":")
    if not tag or "/" in tag:
        return True  # no tag at all means :latest
    return version_from_image_tag(image) is None


def _cached_version(cache: Cache, key: str, force: bool) -> str | None:
    if force:
        return None
    meta = cache.read_meta(key)
    if not meta:
        return None
    return meta.get("version")


def _write_meta(
    cache: Cache,
    key: str,
    source: Source,
    version: str | None,
    *,
    docker_image: str | None = None,
    binary_path: Path | None = None,
    artifact_sha256: str | None = None,
) -> None:
    cache.write_meta(
        key,
        {
            "source": source.raw,
            "kind": source.kind.value,
            "program": PROGRAM,
            "version": version,
            "docker_image": docker_image,
            "binary_path": str(binary_path) if binary_path else None,
            "artifact_sha256": artifact_sha256,
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
        raise ResolveError(
            f"could not pull {image}: {result.stderr.strip() or result.stdout.strip()}"
        )


def _run(command: list[str], *, timeout: int = 60) -> str | None:
    """Run a command, returning its combined output, or None if it could not succeed."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return f"{result.stdout}\n{result.stderr}"
