from __future__ import annotations

import time
from pathlib import Path

import pytest

from chlab.core.cache import Cache, cache_key
from chlab.core.resolve import ResolveError, resolve
from chlab.core.sources import parse_source
from tests.conftest import FAKE_VERSION


def test_resolves_local_binary_in_place(fake_binary: Path, cache: Cache):
    resolved = resolve(str(fake_binary), cache=cache)
    assert resolved.binary_path == fake_binary.resolve()
    assert resolved.version == FAKE_VERSION
    assert resolved.docker_image is None
    assert not resolved.runs_as_image


def test_resolves_local_deb(fake_deb: Path, cache: Cache):
    resolved = resolve(str(fake_deb), cache=cache)
    assert resolved.version == FAKE_VERSION
    assert resolved.binary_path is not None
    assert resolved.binary_path.name == "clickhouse"
    assert resolved.binary_path.is_relative_to(cache.root)


def test_resolves_local_tgz(fake_tgz: Path, cache: Cache):
    resolved = resolve(str(fake_tgz), cache=cache)
    assert resolved.version == FAKE_VERSION


def test_second_resolve_is_cached(fake_deb: Path, cache: Cache):
    first = resolve(str(fake_deb), cache=cache)
    second = resolve(str(fake_deb), cache=cache)
    assert not first.cached
    assert second.cached
    assert second.binary_path == first.binary_path


def test_version_override_skips_detection(tmp_path: Path, cache: Cache):
    """A binary that cannot report its version still resolves when told what it is."""
    binary = tmp_path / "clickhouse"
    binary.write_bytes(b"\x7fELF not really")
    binary.chmod(0o755)

    resolved = resolve(str(binary), version="26.3.17.20001", cache=cache)
    assert resolved.version == "26.3.17.20001"


def test_undetectable_version_is_an_error(tmp_path: Path, cache: Cache, monkeypatch):
    monkeypatch.setattr("chlab.core.resolve._docker_available", lambda: False)
    binary = tmp_path / "clickhouse"
    binary.write_bytes(b"\x7fELF not really")
    binary.chmod(0o755)

    with pytest.raises(ResolveError, match="--clickhouse-version"):
        resolve(str(binary), cache=cache)


def test_missing_local_file_is_an_error(tmp_path: Path, cache: Cache):
    with pytest.raises(ResolveError, match="does not exist"):
        resolve(str(tmp_path / "nope"), cache=cache)


def test_directory_is_an_error(tmp_path: Path, cache: Cache):
    with pytest.raises(ResolveError, match="is not a file"):
        resolve(str(tmp_path), cache=cache)


def test_meta_records_the_resolution(fake_deb: Path, cache: Cache):
    resolve(str(fake_deb), cache=cache)
    meta = cache.read_meta(cache_key(parse_source(str(fake_deb))))
    assert meta is not None
    assert meta["version"] == FAKE_VERSION
    assert meta["source"] == str(fake_deb)
    assert meta["kind"] == "path"


def test_cache_key_tracks_local_file_mutation(fake_binary: Path):
    before = cache_key(parse_source(str(fake_binary)))
    time.sleep(1.1)  # mtime has one-second resolution
    fake_binary.write_bytes(b"#!/bin/sh\necho rebuilt\n")
    assert cache_key(parse_source(str(fake_binary))) != before


def test_cache_key_is_stable_for_urls():
    url = "https://example.com/clickhouse.deb"
    assert cache_key(parse_source(url)) == cache_key(parse_source(url))
