"""Trust boundaries: what runs where, what is verified, and what a reproduction may touch.

One test group per finding of the 2026-11-19 review (C-01, H-01, …, R-01 for reproductions).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from chlab.cli import app
from chlab.core import confine, repro
from chlab.core import resolve as resolve_module
from chlab.core.cache import Cache
from chlab.core.download import ChecksumError, DownloadError, _NoDowngrade
from chlab.core.registry import Registry
from chlab.core.resolve import ResolveError, resolve
from chlab.core.runtime import ClusterError, Runtime
from chlab.core.seed import database_statement
from chlab.core.spec import ClusterSpec, SpecError
from chlab.web.app import create_app
from tests.conftest import FAKE_VERSION
from tests.test_download import http_root  # noqa: F401  (fixture)

cli = CliRunner()


# --- C-01: an untrusted binary is never executed on the host ------------------------------


@pytest.fixture
def probes(monkeypatch):
    """Record which probes run; the container one 'fails', as a hostile binary would make it."""
    calls: list[str] = []

    def host(binary):
        calls.append("host")
        return f"ClickHouse server version {FAKE_VERSION}"

    def container(binary):
        calls.append("container")
        return None

    monkeypatch.setattr(resolve_module, "_host_probe", host)
    monkeypatch.setattr(resolve_module, "_container_probe", container)
    return calls


def test_a_downloaded_binary_never_falls_back_to_the_host(
    http_root,  # noqa: F811
    fake_binary: Path,
    cache: Cache,
    probes,
):
    root, base = http_root
    (root / "clickhouse").write_bytes(fake_binary.read_bytes())
    with pytest.raises(ResolveError, match="--clickhouse-version"):
        resolve(f"{base}/clickhouse", cache=cache)
    assert probes == ["container"]


def test_an_untrusted_local_path_runs_only_in_a_container(fake_binary, cache, probes):
    with pytest.raises(ResolveError):
        resolve(str(fake_binary), cache=cache, trust_local=False)
    assert probes == ["container"]


def test_a_local_path_from_the_user_is_probed_on_the_host(fake_binary, cache, probes):
    assert resolve(str(fake_binary), cache=cache).version == FAKE_VERSION
    assert probes == ["host"]


# --- H-01: --sha256 holds on a warm cache -------------------------------------------------


@pytest.fixture
def served_deb(http_root, fake_deb: Path, monkeypatch):  # noqa: F811
    root, base = http_root
    (root / fake_deb.name).write_bytes(fake_deb.read_bytes())
    # The fake binary is a shell script; read its version on the host.
    monkeypatch.setattr(resolve_module, "_container_probe", resolve_module._host_probe)
    return f"{base}/{fake_deb.name}", hashlib.sha256(fake_deb.read_bytes()).hexdigest()


def test_sha256_is_checked_against_a_cache_resolved_without_it(served_deb, cache: Cache):
    url, digest = served_deb
    first = resolve(url, cache=cache)
    first.binary_path.write_bytes(b"#!/bin/sh\necho tampered\n")

    with pytest.raises(ChecksumError):
        resolve(url, cache=cache, sha256="0" * 64)
    verified = resolve(url, cache=cache, sha256=digest)
    assert not verified.cached
    assert b"tampered" not in verified.binary_path.read_bytes()
    # Once verified against this digest, the cache is trusted for it.
    assert resolve(url, cache=cache, sha256=digest).cached


def test_a_tampered_cached_artifact_is_downloaded_again(served_deb, cache: Cache):
    url, digest = served_deb
    resolve(url, cache=cache)
    (artifact,) = cache.root.glob("artifacts/*/*.deb")
    artifact.write_bytes(b"poisoned")
    resolved = resolve(url, cache=cache, sha256=digest)
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == digest
    assert resolved.version == FAKE_VERSION


def test_a_cache_holding_only_the_extracted_binary_is_refetched(served_deb, cache: Cache):
    url, digest = served_deb
    resolve(url, cache=cache)
    for artifact in cache.root.glob("artifacts/*/*"):
        artifact.unlink()
    assert not resolve(url, cache=cache, sha256=digest).cached


def test_sha256_applies_to_a_local_binary(fake_binary: Path, cache: Cache):
    with pytest.raises(ChecksumError):
        resolve(str(fake_binary), cache=cache, sha256="0" * 64)


# --- M-02: no silent https -> http downgrade ----------------------------------------------


def test_https_redirect_to_http_is_refused():
    import urllib.request

    request = urllib.request.Request("https://example.com/clickhouse")
    with pytest.raises(DownloadError, match="not https"):
        _NoDowngrade().redirect_request(
            request, None, 302, "Found", {}, "http://example.com/clickhouse"
        )


# --- H-02 / L-03 / M-01: the web UI -------------------------------------------------------


@pytest.fixture
def web(tmp_path: Path, monkeypatch):
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    runtime.registry.save(ClusterSpec(name="lab", source="/x", version="24.8", port_base=21000))
    destroyed: list[str] = []
    monkeypatch.setattr(runtime, "destroy", lambda name, **_: destroyed.append(name))
    return TestClient(create_app(runtime), base_url="http://127.0.0.1:7777"), destroyed


def test_a_cross_site_form_cannot_destroy_an_instance(web):
    client, destroyed = web
    response = client.post(
        "/api/instances/lab/actions/destroy",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 403
    assert destroyed == []


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Chlab-Request": "1", "Sec-Fetch-Site": "cross-site"},
        {"X-Chlab-Request": "1", "Sec-Fetch-Site": "same-site"},
        {"X-Chlab-Request": "1", "Origin": "http://evil.example.com"},
    ],
)
def test_cross_site_requests_are_refused_even_with_the_header(web, headers):
    client, destroyed = web
    assert client.post("/api/instances/lab/actions/destroy", headers=headers).status_code == 403
    assert destroyed == []


def test_the_ui_own_requests_are_accepted(web):
    client, destroyed = web
    response = client.post(
        "/api/instances/lab/actions/destroy",
        headers={
            "X-Chlab-Request": "1",
            "Origin": "http://127.0.0.1:7777",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert response.status_code == 200


def test_pages_carry_security_headers(web):
    client, _ = web
    headers = client.get("/").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


def test_the_ui_refuses_a_non_loopback_bind(monkeypatch):
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: pytest.fail("must not serve"))
    result = cli.invoke(app, ["ui", "--host", "0.0.0.0", "--no-open"])
    assert result.exit_code != 0
    assert "loopback" in result.output


def test_the_ui_script_sends_the_header():
    script = (Path(__file__).parents[1] / "chlab/web/static/app.js").read_text()
    assert '"X-Chlab-Request": "1"' in script


# --- M-04: destroy keeps the definition when Docker cleanup fails ------------------------


def test_destroy_keeps_the_definition_when_compose_fails(tmp_path: Path, monkeypatch):
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    runtime.registry.save(ClusterSpec(name="lab", source="/x", port_base=21000))

    def failing(self, spec, *args, **kwargs):
        raise ClusterError("docker compose failed: permission denied")

    monkeypatch.setattr(Runtime, "_compose", failing)
    with pytest.raises(ClusterError, match="definition is kept"):
        runtime.destroy("lab")
    assert runtime.registry.exists("lab")


# --- M-06: generated config and SQL ------------------------------------------------------


@pytest.mark.parametrize("name", ["my cluster", "a<b", "1abc", "x'y", ""])
def test_cluster_names_must_be_identifiers(name):
    with pytest.raises(SpecError, match="cluster name"):
        ClusterSpec(name="lab", source="/x", cluster_name=name)


def test_replicated_database_path_is_a_quoted_literal():
    statement = database_statement("we'ird", "Replicated")
    assert "Replicated('/clickhouse/databases/we\\'ird', '{shard}', '{replica}')" in statement


# --- L-02: cache key precision ------------------------------------------------------------


def test_cache_key_sees_a_same_second_rebuild(fake_binary: Path):
    from chlab.core.cache import cache_key
    from chlab.core.sources import parse_source

    before = cache_key(parse_source(str(fake_binary)))
    stat = fake_binary.stat()
    os.utime(fake_binary, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000))
    assert cache_key(parse_source(str(fake_binary))) != before


# --- R-01: a reproduction's .chlab steps are confined -------------------------------------


@pytest.fixture
def confined(tmp_path: Path, monkeypatch):
    """A saved instance `lab`, a second one `other`, and the CLI confined to `lab` and ./r."""
    monkeypatch.setenv("CHLAB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHLAB_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr("chlab.core.runtime._compose_containers", lambda: {})
    registry = Registry()
    registry.save(ClusterSpec(name="lab", source="/x", port_base=21000, services=("s3",)))
    registry.save(ClusterSpec(name="other", source="/x", port_base=22000))
    directory = tmp_path / "r"
    directory.mkdir()
    for key, value in confine.environment("lab", directory).items():
        monkeypatch.setenv(key, value)
    monkeypatch.chdir(directory)
    calls: list[list[str]] = []
    monkeypatch.setattr("subprocess.call", lambda command, **_: calls.append(command) or 0)
    return directory, calls


def test_a_step_cannot_name_another_instance(confined):
    _, calls = confined
    for args in (["stop", "other"], ["exec", "other", "--", "id"], ["cp", "other:/etc/x", "./x"]):
        result = cli.invoke(app, args)
        assert result.exit_code != 0, args
    assert calls == []


@pytest.mark.parametrize(
    "args",
    [
        ["cp", "lab:/tmp/payload", "{home}/.bashrc"],
        ["cp", "{home}/.ssh/id_rsa", "lab:/tmp/"],
        ["s3", "get", "lab", "warehouse/x", "{home}/x"],
        ["s3", "put", "lab", "{home}/.ssh", "warehouse/x/"],
        ["query", "lab", "-f", "{home}/secret.sql"],
        ["py", "lab", "{home}/script.py"],
        ["cp", "lab:/tmp/payload", "./escape/x"],
    ],
)
def test_a_step_cannot_reach_outside_its_directory(confined, tmp_path, args):
    directory, calls = confined
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / "secret.sql").write_text("SELECT 1")
    (home / "script.py").write_text("print(1)")
    (directory / "escape").symlink_to(home)  # a symlink inside does not lead out
    result = cli.invoke(app, [arg.replace("{home}", str(home)) for arg in args])
    assert result.exit_code != 0, result.output
    assert "outside the reproduction directory" in result.output
    assert calls == []


def test_a_step_may_use_its_own_directory(confined):
    directory, calls = confined
    (directory / "data.parquet").write_bytes(b"x")
    assert cli.invoke(app, ["cp", "./data.parquet", "lab:/tmp/"]).exit_code == 0
    assert cli.invoke(app, ["cp", "lab:/tmp/out", "./out"]).exit_code == 0
    assert len(calls) == 2


def test_chlab_steps_run_confined(tmp_path: Path, monkeypatch):
    from tests.test_repro import FakeRuntime, minimal

    seen = {}

    def run(command, env=None, stdin=None, **kwargs):
        seen.update(env=env, stdin=stdin)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("chlab.core.repro.subprocess.run", run)
    directory = minimal(tmp_path, **{"01.chlab": "restart"})
    result = repro.Runner(FakeRuntime(Registry(tmp_path / "state"))).run(
        repro.load(directory), ["/b/a"]
    )
    env = seen["env"]
    assert env[confine.ENV_INSTANCE] == result.builds[0].instance
    assert env[confine.ENV_DIRECTORY] == str(directory.resolve())
    assert seen["stdin"] is subprocess.DEVNULL


def test_builds_named_by_repro_yaml_are_untrusted(tmp_path: Path):
    from tests.test_repro import FakeRuntime, minimal

    runtime = FakeRuntime(Registry(tmp_path / "state"))
    directory = minimal(tmp_path, **{"01.chlab": "restart"})
    repro.Runner(runtime).run(repro.load(directory), ["/b/a", "/b/b"], untrusted=["/b/b"])
    assert runtime.trusted == {"/b/a": True, "/b/b": False}


def test_the_cli_marks_only_yaml_builds_untrusted(tmp_path: Path, monkeypatch):
    from tests.test_repro import _run

    seen = {}

    def fake_run(self, loaded, sources, keep=False, untrusted=()):
        seen["untrusted"] = list(untrusted)
        return _run(sources)

    monkeypatch.setattr(repro.Runner, "run", fake_run)
    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "repro.yaml").write_text("clickhouse: ./evil\ncompare: /b/base\n")
    (directory / "01.sql").write_text("SELECT 1")
    out = tmp_path / "o"
    cli.invoke(app, ["repro", "run", str(directory), "--out", str(out)])
    assert seen["untrusted"] == ["./evil", "/b/base"]
    cli.invoke(app, ["repro", "run", str(directory), "-c", "/b/pr", "--out", str(out)])
    assert seen["untrusted"] == ["/b/base"]


def test_results_do_not_follow_a_planted_symlink(tmp_path: Path):
    from tests.test_repro import _run

    run = _run(["/b/a"])
    directory = tmp_path / "r"
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    stamp = run.started.replace(":", "").replace("-", "")
    (directory / "results" / stamp).mkdir(parents=True)
    (directory / "results" / stamp / "report.md").symlink_to(victim)

    destination = repro.write_results(run, directory, None)
    assert victim.read_text() == "keep me"
    assert not (destination / "report.md").is_symlink()


def test_results_directory_must_stay_inside(tmp_path: Path):
    from tests.test_repro import _run

    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "results").symlink_to(tmp_path)
    with pytest.raises(repro.ReproError, match="leads outside"):
        repro.write_results(_run(["/b/a"]), directory, None)
