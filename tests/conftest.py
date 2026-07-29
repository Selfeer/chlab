from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest

from chlab.core.cache import Cache

FAKE_VERSION = "24.8.14.10501"
FAKE_BINARY = f"""#!/bin/sh
echo "ClickHouse server version {FAKE_VERSION}."
""".encode()


@pytest.fixture
def cache(tmp_path: Path) -> Cache:
    return Cache(tmp_path / "cache")


@pytest.fixture
def fake_binary(tmp_path: Path) -> Path:
    """A stand-in for the ClickHouse binary that answers `server --version`."""
    path = tmp_path / "clickhouse"
    path.write_bytes(FAKE_BINARY)
    path.chmod(0o755)
    return path


def make_tar_gz(members: dict[str, bytes]) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o755
            archive.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue())


def make_ar(members: dict[str, bytes]) -> bytes:
    out = bytearray(b"!<arch>\n")
    for name, content in members.items():
        out += name.ljust(16).encode()
        out += b"0".ljust(12)  # mtime
        out += b"0".ljust(6)  # uid
        out += b"0".ljust(6)  # gid
        out += b"100644".ljust(8)  # mode
        out += str(len(content)).ljust(10).encode()
        out += b"`\n"
        out += content
        if len(content) % 2:
            out += b"\n"
    return bytes(out)


@pytest.fixture
def fake_deb(tmp_path: Path) -> Path:
    """A minimal but structurally real .deb carrying ./usr/bin/clickhouse."""
    data_tar = make_tar_gz({"./usr/bin/clickhouse": FAKE_BINARY, "./usr/bin/other": b"x"})
    path = tmp_path / "clickhouse-common-static_24.8.14.10501_amd64.deb"
    path.write_bytes(
        make_ar(
            {
                "debian-binary": b"2.0\n",
                "control.tar.gz": make_tar_gz({"./control": b"Package: clickhouse\n"}),
                "data.tar.gz": data_tar,
            }
        )
    )
    return path


@pytest.fixture
def fake_tgz(tmp_path: Path) -> Path:
    """A .tgz laid out the way ClickHouse release tarballs are: nested under a version dir."""
    prefix = "clickhouse-common-static-24.8.14.10501"
    path = tmp_path / "clickhouse-common-static-24.8.14.10501.tgz"
    payload = make_tar_gz(
        {f"{prefix}/usr/bin/clickhouse": FAKE_BINARY, f"{prefix}/usr/bin/other": b"x"}
    )
    path.write_bytes(payload)
    return path


def pytest_addoption(parser):
    parser.addoption(
        "--run-docker",
        action="store_true",
        default=False,
        help="Run tests that launch real containers.",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs a working Docker daemon")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-docker"):
        return
    skip = pytest.mark.skip(reason="needs --run-docker")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
