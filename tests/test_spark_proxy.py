"""Spark and toxiproxy: configuration, jars, the toxiproxy API, CLI and repro steps.

Downloads and the toxiproxy API are served locally; no containers are started.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from chlab.cli import app
from chlab.core import compose, configgen, repro
from chlab.core.cache import Cache
from chlab.core.registry import Registry
from chlab.core.resolve import Resolved
from chlab.core.services import spark, toxiproxy
from chlab.core.sources import parse_source
from chlab.core.spec import ClusterSpec, SpecError
from chlab.core.topology import ServiceNode, build_topology

LAKE = ("s3", "iceberg-rest", "ice-rest", "spark", "toxiproxy")


def spec(**overrides) -> ClusterSpec:
    fields = {"name": "lake", "source": "/bin/clickhouse", "port_base": 21000, "services": LAKE}
    fields.update(overrides)
    return ClusterSpec(**fields)


def render(tmp_path: Path, lake: ClusterSpec | None = None) -> dict:
    topology = build_topology(lake or spec())
    configgen.write_configs(topology, tmp_path, base_image_mode=False)
    resolved = Resolved(source=parse_source("/bin/clickhouse"), version="1", binary_path=Path("/x"))
    return compose.render(topology, resolved, tmp_path, "img")


# --- spec ----------------------------------------------------------------------------------


def test_spark_needs_a_catalog():
    with pytest.raises(SpecError, match="spark needs a catalog"):
        spec(services=("s3", "spark"))


@pytest.mark.parametrize("service", ["spark", "toxiproxy"])
def test_spark_and_toxiproxy_need_s3(service):
    with pytest.raises(SpecError, match=f"{service} needs s3"):
        spec(services=(service, "iceberg-rest"))


# --- spark ---------------------------------------------------------------------------------


def test_spark_defaults_name_catalogs_as_pyiceberg_does(tmp_path: Path):
    render(tmp_path)
    conf = dict(
        line.split(" ", 1)
        for line in (tmp_path / "services/spark/spark-defaults.conf").read_text().splitlines()
    )
    assert conf["spark.sql.defaultCatalog"] == "rest"
    assert conf["spark.sql.catalog.rest.uri"] == "http://iceberg-rest:8181"
    assert conf["spark.sql.catalog.ice.uri"] == "http://ice-rest:5000"
    assert conf["spark.sql.catalog.ice.token"] == "foo"
    assert "spark.sql.catalog.rest.token" not in conf
    assert conf["spark.sql.catalog.rest.s3.path-style-access"] == "true"
    assert conf["spark.sql.catalog.rest.s3.endpoint"] == "http://s3:9000"


def test_spark_overlays_the_1_9_1_jars_and_skips_the_image_entrypoint(tmp_path: Path):
    service = render(tmp_path)["services"]["spark"]
    assert service["image"].startswith("tabulario/spark-iceberg@sha256:")
    assert service["entrypoint"] == ["tail", "-f", "/dev/null"]
    mounts = service["volumes"]
    assert any(
        m.endswith(":/opt/spark/jars/iceberg-spark-runtime-3.5_2.12-1.8.1.jar:ro") for m in mounts
    )
    assert any(m.endswith(":/opt/spark/jars/iceberg-aws-bundle-1.8.1.jar:ro") for m in mounts)
    assert service["depends_on"] == ["s3", "iceberg-rest", "ice-rest"]


class _Jars(http.server.BaseHTTPRequestHandler):
    served: list[str] = []

    def do_GET(self):
        body = self.path.encode()
        type(self).served.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_prepare_downloads_verified_jars_once_and_links_them(tmp_path: Path, monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Jars)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/maven"
        overlays = tuple(
            (replaced, path, hashlib.sha256(f"/maven/{path}".encode()).hexdigest())
            for replaced, path, _ in spark.JAR_OVERLAYS
        )
        monkeypatch.setattr(spark, "MAVEN", base)
        monkeypatch.setattr(spark, "JAR_OVERLAYS", overlays)
        cache = Cache(tmp_path / "cache")
        topology = build_topology(spec())
        for instance in ("a", "b"):
            spark.Spark().prepare(topology, tmp_path / instance, cache, lambda _m: None)
        assert len(_Jars.served) == 2  # the second instance reuses the cache
        linked = tmp_path / "b/services/spark/jars/iceberg-aws-bundle-1.8.1.jar"
        assert linked.read_bytes().startswith(b"/maven/iceberg-aws-bundle")
    finally:
        server.shutdown()
        server.server_close()


def test_a_jar_with_the_wrong_checksum_is_refused(tmp_path: Path, monkeypatch):
    from chlab.core.download import ChecksumError

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Jars)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(spark, "MAVEN", f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(ChecksumError):
            spark.Spark().prepare(
                build_topology(spec()), tmp_path, Cache(tmp_path / "c"), lambda _m: None
            )
    finally:
        server.shutdown()
        server.server_close()


def test_spark_sql_reads_statements_from_stdin_into_a_file():
    command = spark.spark_sql_command(spec())
    assert command[:4] == ["docker", "exec", "-i", "chlab-lake-spark"]
    assert "spark-sql -S -f" in command[-1]


# --- toxiproxy -----------------------------------------------------------------------------


def test_toxiproxy_forwards_to_s3_and_gets_its_own_collections(tmp_path: Path):
    import xml.etree.ElementTree as ET

    document = render(tmp_path, spec(buckets=("warehouse", "other")))
    config = json.loads((tmp_path / "services/toxiproxy/toxiproxy.json").read_text())
    assert config == [
        {"name": "s3", "listen": "0.0.0.0:9100", "upstream": "s3:9000", "enabled": True}
    ]
    assert document["services"]["toxiproxy"]["ports"] == [
        "127.0.0.1:21430:8474",
        "127.0.0.1:21431:9100",
    ]
    root = ET.parse(tmp_path / "configs/clickhouse1/config.d/61-chlab-toxiproxy.xml").getroot()
    collections = root.find("named_collections")
    assert [c.tag for c in collections] == ["s3_proxy", "s3_proxy_warehouse", "s3_proxy_other"]
    assert collections.findtext("s3_proxy_other/url") == "http://toxiproxy:9100/other/"


class _Toxiproxy(http.server.BaseHTTPRequestHandler):
    """Just enough of the toxiproxy API: one proxy, its toxics, enable and reset."""

    state = {"enabled": True, "toxics": []}
    calls: list[tuple[str, str, object]] = []

    def _reply(self, payload, status=200):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(length)) if length else None

    def do_GET(self):
        self._reply({"name": "s3", "listen": "[::]:9100", "upstream": "s3:9000", **self.state})

    def do_POST(self):
        body = self._body()
        self.calls.append(("POST", self.path, body))
        if self.path.endswith("/toxics"):
            self.state["toxics"].append(body)
        elif self.path == "/reset":
            self.state.update(enabled=True, toxics=[])
        else:
            self.state["enabled"] = body["enabled"]
        self._reply({})

    def do_DELETE(self):
        self.calls.append(("DELETE", self.path, None))
        name = self.path.rsplit("/", 1)[1]
        self.state["toxics"] = [t for t in self.state["toxics"] if t["name"] != name]
        self._reply(None, 204)

    def log_message(self, *args):
        pass


@pytest.fixture
def proxy_node():
    handler = type("H", (_Toxiproxy,), {"state": {"enabled": True, "toxics": []}, "calls": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield handler, ServiceNode("toxiproxy", server.server_port)
    finally:
        server.shutdown()
        server.server_close()


def test_a_fault_replaces_the_earlier_one_of_its_kind(proxy_node):
    handler, node = proxy_node
    toxiproxy.set_toxic(node, "latency", 100)
    toxiproxy.set_toxic(node, "latency", 500, jitter=50)
    toxics = toxiproxy.status(node)["toxics"]
    assert toxics == [
        {
            "name": "latency_downstream",
            "type": "latency",
            "stream": "downstream",
            "latency": 500,
            "jitter": 50,
        }
    ]
    assert ("DELETE", "/proxies/s3/toxics/latency_downstream", None) in handler.calls


def test_upstream_and_downstream_faults_coexist(proxy_node):
    _, node = proxy_node
    toxiproxy.set_toxic(node, "bandwidth", 10)
    toxiproxy.set_toxic(node, "bandwidth", 20, upstream=True)
    assert {t["stream"] for t in toxiproxy.status(node)["toxics"]} == {"downstream", "upstream"}


def test_down_and_reset(proxy_node):
    _, node = proxy_node
    toxiproxy.set_toxic(node, "timeout", 0)
    toxiproxy.set_enabled(node, False)
    assert toxiproxy.status(node)["enabled"] is False
    toxiproxy.reset(node)
    assert toxiproxy.status(node) == {
        "enabled": True,
        "listen": "[::]:9100",
        "upstream": "s3:9000",
        "toxics": [],
    }


def test_an_unreachable_proxy_says_so():
    with pytest.raises(toxiproxy.ProxyError, match="not reachable"):
        toxiproxy.status(ServiceNode("toxiproxy", 1))


def test_unknown_faults_are_refused(proxy_node):
    with pytest.raises(toxiproxy.ProxyError, match="unknown fault"):
        toxiproxy.set_toxic(proxy_node[1], "chaos", 1)


# --- CLI -----------------------------------------------------------------------------------


runner = CliRunner()


@pytest.fixture
def lake(tmp_path: Path, monkeypatch) -> ClusterSpec:
    monkeypatch.setenv("CHLAB_STATE_DIR", str(tmp_path / "state"))
    instance = spec()
    Registry().save(instance)
    Registry().save(ClusterSpec(name="plain", source="/x", port_base=22000))
    return instance


def test_spark_command_feeds_statements_on_stdin(lake, monkeypatch):
    import subprocess

    seen = {}

    def run(command, input, text):
        seen.update(command=command, input=input)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr("subprocess.run", run)
    assert runner.invoke(app, ["spark", "lake", "SELECT 1; SELECT 2"]).exit_code == 0
    assert seen["input"] == "SELECT 1; SELECT 2" and seen["command"][3] == "chlab-lake-spark"


@pytest.mark.parametrize("args", [["spark", "plain", "SELECT 1"], ["proxy", "status", "plain"]])
def test_commands_explain_a_missing_service(lake, args):
    result = runner.invoke(app, args)
    assert result.exit_code == 1 and "launch it with --with" in result.output


def test_proxy_commands_drive_the_api(lake, monkeypatch):
    calls = []
    monkeypatch.setattr(
        toxiproxy, "set_toxic", lambda node, kind, value, **kw: calls.append((kind, value, kw))
    )
    monkeypatch.setattr(
        toxiproxy, "set_enabled", lambda node, enabled: calls.append(("enabled", enabled))
    )
    monkeypatch.setattr(toxiproxy, "reset", lambda node: calls.append(("reset",)))
    for args in (
        ["latency", "lake", "300", "--jitter", "20"],
        ["bandwidth", "lake", "64", "--upstream"],
        ["timeout", "lake", "0"],
        ["down", "lake"],
        ["up", "lake"],
        ["reset", "lake"],
    ):
        assert runner.invoke(app, ["proxy", *args]).exit_code == 0
    assert calls == [
        ("latency", 300, {"jitter": 20, "upstream": False}),
        ("bandwidth", 64, {"upstream": True}),
        ("timeout", 0, {"upstream": False}),
        ("enabled", False),
        ("enabled", True),
        ("reset",),
    ]


# --- repro ---------------------------------------------------------------------------------


def test_spark_steps_are_recognised(tmp_path: Path):
    directory = tmp_path / "r"
    directory.mkdir()
    (directory / "repro.yaml").write_text("with: [s3, iceberg-rest, spark]\n")
    (directory / "01-v3.spark").write_text("-- chlab: expect contains 900\nSELECT 900;")
    step = repro.load(directory).steps[0]
    assert (step.kind, step.where, step.expect.contains) == ("spark", "spark", ("900",))


def test_spark_steps_take_no_node(tmp_path: Path):
    (tmp_path / "s.spark").write_text("-- chlab: node clickhouse1\n")
    with pytest.raises(repro.ReproError, match="only"):
        repro.parse_step(tmp_path / "s.spark")


def test_proxy_lines_in_chlab_steps_get_the_instance_after_the_subcommand():
    script = repro._chlab_script("lab", "proxy latency 500\nproxy reset\nspark 'SELECT 1'\n")[-1]
    assert "chlab.cli proxy latency lab 500" in script
    assert "chlab.cli proxy reset lab" in script
    assert "chlab.cli spark lab 'SELECT 1'" in script
