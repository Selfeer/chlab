from __future__ import annotations

import os
from pathlib import Path

import pytest

from chlab.core.unpack import UnpackError, extract_binary
from tests.conftest import FAKE_BINARY, make_ar, make_tar_gz


def test_extracts_binary_from_deb(fake_deb: Path, tmp_path: Path):
    dest = tmp_path / "out" / "clickhouse"
    extract_binary(fake_deb, "clickhouse", dest)
    assert dest.read_bytes() == FAKE_BINARY
    assert os.access(dest, os.X_OK)


def test_extracts_binary_from_tgz(fake_tgz: Path, tmp_path: Path):
    dest = tmp_path / "out" / "clickhouse"
    extract_binary(fake_tgz, "clickhouse", dest)
    assert dest.read_bytes() == FAKE_BINARY


def test_rejects_unknown_package_format(tmp_path: Path):
    package = tmp_path / "clickhouse.zip"
    package.write_bytes(b"PK")
    with pytest.raises(UnpackError, match="don't know how to unpack"):
        extract_binary(package, "clickhouse", tmp_path / "out")


def test_rejects_non_ar_deb(tmp_path: Path):
    package = tmp_path / "broken.deb"
    package.write_bytes(b"not an archive at all")
    with pytest.raises(UnpackError, match="bad ar magic"):
        extract_binary(package, "clickhouse", tmp_path / "out")


def test_reports_missing_data_member(tmp_path: Path):
    package = tmp_path / "nodata.deb"
    package.write_bytes(make_ar({"debian-binary": b"2.0\n"}))
    with pytest.raises(UnpackError, match="no data.tar"):
        extract_binary(package, "clickhouse", tmp_path / "out")


def test_reports_missing_binary(tmp_path: Path):
    package = tmp_path / "empty.deb"
    package.write_bytes(
        make_ar(
            {"debian-binary": b"2.0\n", "data.tar.gz": make_tar_gz({"./etc/config.xml": b"<x/>"})}
        )
    )
    with pytest.raises(UnpackError, match="does not contain"):
        extract_binary(package, "clickhouse", tmp_path / "out")


def test_handles_odd_sized_ar_members(tmp_path: Path):
    """ar pads members to an even offset; a one-byte control member exercises that path."""
    package = tmp_path / "odd.deb"
    package.write_bytes(
        make_ar(
            {
                "debian-binary": b"x",  # odd length, forces padding
                "data.tar.gz": make_tar_gz({"./usr/bin/clickhouse": FAKE_BINARY}),
            }
        )
    )
    dest = tmp_path / "out" / "clickhouse"
    extract_binary(package, "clickhouse", dest)
    assert dest.read_bytes() == FAKE_BINARY
