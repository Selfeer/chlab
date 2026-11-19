"""End-to-end URL resolution against a local HTTP server.

Covers the path that matters most in practice: a build-artifact URL pointing at a .deb.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import threading
from pathlib import Path

import pytest

from chlab.core.cache import Cache
from chlab.core.download import ChecksumError, DownloadError, download
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


@pytest.fixture(autouse=True)
def container_probe_on_host(monkeypatch):
    """A downloaded binary's version is read only in a container. The fakes here are shell
    scripts, so stand the container in with the host rather than need Docker for unit tests."""
    from chlab.core import resolve as resolve_module

    monkeypatch.setattr(resolve_module, "_container_probe", resolve_module._host_probe)


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
    download(
        f"{base}/payload.bin", tmp_path / "payload.bin", on_progress=lambda *args: seen.append(args)
    )

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


def test_truncated_download_is_rejected_not_cached(tmp_path: Path):
    """http.client does not raise on a short body, so a dropped transfer looked complete."""
    import http.server
    import threading

    class Liar(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"0123456789")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Liar)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        dest = tmp_path / "short.deb"
        with pytest.raises(DownloadError, match="cut short"):
            download(f"http://127.0.0.1:{server.server_port}/x", dest)
        # Critically, the truncated file must not be left where the cache would reuse it.
        assert not dest.exists()
        assert not dest.with_name(dest.name + ".part").exists()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_checksum_mismatch_rejects_the_artifact(http_root, tmp_path: Path):
    root, base = http_root
    (root / "payload.bin").write_bytes(b"real content")
    with pytest.raises(ChecksumError, match="expected"):
        download(f"{base}/payload.bin", tmp_path / "payload.bin", sha256="0" * 64)
    assert not (tmp_path / "payload.bin").exists()


def test_matching_checksum_is_accepted(http_root, tmp_path: Path):
    root, base = http_root
    (root / "payload.bin").write_bytes(b"real content")
    digest = hashlib.sha256(b"real content").hexdigest()
    assert download(f"{base}/payload.bin", tmp_path / "payload.bin", sha256=digest).exists()
