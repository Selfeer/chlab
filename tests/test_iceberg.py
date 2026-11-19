"""Iceberg catalogs, the tools container and the chlab_tools helper — no containers started."""

from __future__ import annotations

import http.server
import importlib
import json
import sys
import threading
from pathlib import Path

import pytest
import yaml

from chlab.core import compose, configgen, tools
from chlab.core.image import TOOLS_DOCKERFILE, tools_image_tag
from chlab.core.resolve import Resolved
from chlab.core.services import REGISTRY, s3
from chlab.core.services.catalogs import IcebergRestFixture, IceRestCatalog
from chlab.core.sources import parse_source
from chlab.core.spec import ClusterSpec, SpecError
from chlab.core.topology import build_topology

ALL = ("s3", "iceberg-rest", "ice-rest")


def spec(**overrides) -> ClusterSpec:
    fields = {"name": "lake", "source": "/bin/clickhouse", "port_base": 21000, "services": ALL}
    fields.update(overrides)
    return ClusterSpec(**fields)


def _resolved() -> Resolved:
    return Resolved(source=parse_source("/bin/clickhouse"), version="1", binary_path=Path("/x"))


# --- spec ----------------------------------------------------------------------------------


@pytest.mark.parametrize("catalog", ["iceberg-rest", "ice-rest"])
def test_catalogs_need_object_storage(catalog):
    with pytest.raises(SpecError, match=f"{catalog} needs s3"):
        spec(services=(catalog,))


def test_every_catalog_is_registered():
    assert {"iceberg-rest", "ice-rest"} <= set(REGISTRY)


# --- compose -------------------------------------------------------------------------------


def test_the_rest_fixture_keeps_its_catalog_in_a_file_on_a_volume(tmp_path: Path):
    document = compose.render(build_topology(spec()), _resolved(), tmp_path, "img")
    service = document["services"]["iceberg-rest"]
    assert service["image"] == IcebergRestFixture.image
    assert service["environment"]["CATALOG_URI"].startswith("jdbc:sqlite:file:/data/")
    assert service["environment"]["CATALOG_S3_ENDPOINT"] == "http://s3:9000"
    assert service["environment"]["CATALOG_S3_PATH__STYLE__ACCESS"] == "true"
    assert service["depends_on"] == ["s3"]
    assert "iceberg-rest-data" in document["volumes"]


def test_ice_rest_reads_a_generated_config(tmp_path: Path):
    topology = build_topology(spec(buckets=("lakehouse",)))
    configgen.write_configs(topology, tmp_path, base_image_mode=False)
    config_path = tmp_path / "services/ice-rest/ice-rest-catalog.yaml"
    config = yaml.safe_load(config_path.read_text())
    assert config["warehouse"] == "s3://lakehouse/"
    assert config["s3"]["pathStyleAccess"] is True
    assert config["bearerTokens"] == [{"value": "foo"}]

    service = compose.render(topology, _resolved(), tmp_path, "img")["services"]["ice-rest"]
    assert f"{config_path}:/etc/ice/ice-rest-catalog.yaml:ro" in service["volumes"]


def test_each_catalog_gets_its_own_host_port(tmp_path: Path):
    services = compose.render(build_topology(spec()), _resolved(), tmp_path, "img")["services"]
    assert services["iceberg-rest"]["ports"] == ["127.0.0.1:21400:8181"]
    assert services["ice-rest"]["ports"] == ["127.0.0.1:21410:5000"]


# --- SQL snippets ----------------------------------------------------------------------------


def test_the_fixture_is_addressed_with_v1_by_clickhouse_but_not_by_pyiceberg():
    lake = spec(version="26.3.1.1")
    fixture = IcebergRestFixture()
    assert "DataLakeCatalog('http://iceberg-rest:8181/v1'" in fixture.database_sql(lake)
    assert (
        fixture.tool_env(None, None)["PYICEBERG_CATALOG__REST__URI"] == "http://iceberg-rest:8181"
    )


def test_ice_rest_sends_its_bearer_token():
    sql = IceRestCatalog().database_sql(spec(version="26.3.1.1"))
    assert "DataLakeCatalog('http://ice-rest:5000'" in sql
    assert "auth_header = 'Authorization: Bearer foo'" in sql
    assert IceRestCatalog().tool_env(None, None)["PYICEBERG_CATALOG__ICE__TOKEN"] == "foo"


@pytest.mark.parametrize(
    "version, engine",
    [("25.2.1.1", "Iceberg"), ("25.3.1.1", "DataLakeCatalog"), (None, "DataLakeCatalog")],
)
def test_the_database_engine_name_follows_the_version(version, engine):
    assert f"ENGINE = {engine}(" in IcebergRestFixture().database_sql(spec(version=version))


@pytest.mark.parametrize(
    "version, function",
    [("24.8.14.1", "iceberg("), ("24.9.1.1", "icebergS3("), (None, "icebergS3(")],
)
def test_catalog_less_reads_follow_the_version(version, function):
    assert function in s3.iceberg_without_catalog_sql(spec(version=version))["iceberg_read"]


@pytest.mark.parametrize(
    "version, bound, expected",
    [
        ("26.3.60.20002.altinityantalya", (25, 3), False),
        ("24.8.14.10501", (24, 9), True),
        ("25.3", (25, 3), False),
        ("head", (25, 3), False),
        ("", (25, 3), False),
    ],
)
def test_version_below(version, bound, expected):
    assert s3.version_below(version, bound) is expected


# --- tools container -----------------------------------------------------------------------


def test_tools_environment_wires_every_service():
    env = tools.environment(spec(shards=2))
    assert json.loads(env["CHLAB_NODES"]) == {
        "clickhouse1": "http://clickhouse1:8123",
        "clickhouse2": "http://clickhouse2:8123",
    }
    assert env["CHLAB_CATALOGS"] == "rest,ice"
    assert env["PYICEBERG_DEFAULT_CATALOG"] == "rest"
    assert env["AWS_ENDPOINT_URL"] == "http://s3:9000"
    assert env["CHLAB_S3_BUCKET"] == "warehouse"
    assert env["PYICEBERG_CATALOG__ICE__S3__ENDPOINT"] == "http://s3:9000"


def test_tools_environment_without_services_still_reaches_clickhouse():
    env = tools.environment(spec(services=()))
    assert "PYICEBERG_DEFAULT_CATALOG" not in env and env["CHLAB_CATALOGS"] == ""
    assert "clickhouse1" in json.loads(env["CHLAB_NODES"])


def test_tools_command_joins_the_network_and_mounts_the_workdir(tmp_path: Path):
    command = tools.tools_command(
        spec(),
        ["python", "/chlab/run/x.py"],
        image="img",
        workdir=tmp_path,
        mounts=[("/host/x.py", "/chlab/run/x.py")],
    )
    assert command[command.index("--network") + 1] == "chlab-lake_default"
    assert f"{tmp_path.resolve()}:/work" in command
    assert "/host/x.py:/chlab/run/x.py:ro" in command
    assert f"{tools.TOOLBOX}:/chlab/lib:ro" in command
    assert command[-3:] == ["img", "python", "/chlab/run/x.py"]


def test_the_toolbox_ships_with_chlab():
    assert (tools.TOOLBOX / "chlab_tools.py").is_file()


def test_tools_image_tag_tracks_its_dockerfile():
    assert tools_image_tag().startswith("chlab/tools:")
    assert "pyiceberg==0.9.0" in TOOLS_DOCKERFILE


# --- chlab_tools ---------------------------------------------------------------------------


class _ClickHouse(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        sql = self.rfile.read(int(self.headers["Content-Length"])).decode()
        if "boom" in sql:
            body, status = b"Code: 60. DB::Exception: Unknown table", 404
        else:
            body, status = b'{"n":1}\n{"n":2}\n', 200
        self.send_response(status)
        if status != 200:
            self.send_header("X-ClickHouse-Exception-Code", "60")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def chlab_tools(monkeypatch):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _ClickHouse)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(
        "CHLAB_NODES", json.dumps({"clickhouse1": f"http://127.0.0.1:{server.server_port}"})
    )
    monkeypatch.setenv("CHLAB_CATALOGS", "")
    monkeypatch.syspath_prepend(str(tools.TOOLBOX))
    sys.modules.pop("chlab_tools", None)
    try:
        yield importlib.import_module("chlab_tools")
    finally:
        sys.modules.pop("chlab_tools", None)
        server.shutdown()
        server.server_close()


def test_rows_returns_dicts(chlab_tools):
    assert chlab_tools.rows("SELECT n FROM t") == [{"n": 1}, {"n": 2}]


def test_query_errors_carry_the_clickhouse_code(chlab_tools):
    with pytest.raises(chlab_tools.QueryError) as failure:
        chlab_tools.query("SELECT boom")
    assert failure.value.code == 60


def test_catalog_without_a_catalog_service_says_how_to_get_one(chlab_tools):
    with pytest.raises(RuntimeError, match="--with iceberg-rest"):
        chlab_tools.catalog()
