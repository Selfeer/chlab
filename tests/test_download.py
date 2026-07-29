"""End-to-end URL resolution against a local HTTP server.

Covers the path that matters most in practice: a build-artifact URL pointing at a .deb.
"""

from __future__ import annotations

import functools
import http.server
import threading
from pathlib import Path

import pytest

from chlab.core.cache import Cache
from chlab.core.download import DownloadError, download
from chlab.core.resolve import resolve
from tests.conftest import FAKE_VERSION


@pytest.fixture
def http_root(tmp_path: Path):
    """Serve ``tmp_path/www`` over HTTP, yielding the base URL."""
    root = tmp_path / "www"
    root.mkdir()
    handler = functools.partial(_QuietHandler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield root, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


def test_resolves_deb_over_http(http_root, fake_deb: Path, cache: Cache):
    root, base = http_root
    (root / fake_deb.name).write_bytes(fake_deb.read_bytes())

    resolved = resolve(f"{base}/{fake_deb.name}", cache=cache)
    assert resolved.version == FAKE_VERSION
    assert resolved.binary_path.is_relative_to(cache.root)

    again = resolve(f"{base}/{fake_deb.name}", cache=cache)
    assert again.cached


def test_resolves_raw_binary_over_http(http_root, fake_binary: Path, cache: Cache):
    root, base = http_root
    (root / "clickhouse").write_bytes(fake_binary.read_bytes())

    resolved = resolve(f"{base}/clickhouse", cache=cache)
    assert resolved.version == FAKE_VERSION
    assert resolved.binary_path.is_relative_to(cache.root)


def test_download_reports_progress(http_root, tmp_path: Path):
    root, base = http_root
    (root / "payload.bin").write_bytes(b"x" * 4096)

    seen: list[tuple[int, int | None]] = []
    download(f"{base}/payload.bin", tmp_path / "payload.bin", on_progress=lambda *args: seen.append(args))

    assert seen[0] == (0, 4096)
    assert seen[-1] == (4096, 4096)


def test_missing_url_is_an_error(http_root, tmp_path: Path):
    _, base = http_root
    with pytest.raises(DownloadError, match="404"):
        download(f"{base}/absent.deb", tmp_path / "absent.deb")


def test_failed_download_leaves_nothing_behind(http_root, tmp_path: Path):
    _, base = http_root
    dest = tmp_path / "absent.deb"
    with pytest.raises(DownloadError):
        download(f"{base}/absent.deb", dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()
