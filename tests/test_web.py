"""Web interface, driven against a runtime whose Docker calls are stubbed out."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from chlab.core.query import QueryError, QueryResult
from chlab.core.registry import Registry
from chlab.core.runtime import Runtime
from chlab.core.spec import ClusterSpec, DatabaseSpec, UserSpec
from chlab.web.app import create_app


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch) -> Runtime:
    runtime = Runtime(registry=Registry(tmp_path / "state"))
    # Nothing in these tests should reach Docker.
    monkeypatch.setattr(Runtime, "_services", lambda self, spec: [])
    monkeypatch.setattr(
        "chlab.core.profiles.default_config_dir", lambda: tmp_path / "config"
    )
    runtime.registry.save(
        ClusterSpec(
            name="lab",
            source="/bin/clickhouse",
            version="24.8.1.1",
            shards=2,
            replicas=2,
            keeper_nodes=1,
            port_base=21000,
            users=(UserSpec("dev", "pw"),),
            databases=(DatabaseSpec("analytics"),),
        )
    )
    return runtime


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    return TestClient(create_app(runtime))


def test_dashboard_lists_instances(client: TestClient):
    response = client.get("/")
    assert response.status_code == 200
    assert "lab" in response.text


def test_new_cluster_page_renders(client: TestClient):
    assert client.get("/new").status_code == 200


def test_instance_page_shows_nodes_and_play_links(client: TestClient):
    body = client.get("/instance/lab").text
    assert "clickhouse4" in body
    assert "http://127.0.0.1:21000/play" in body
    assert "keeper1" in body


def test_unknown_instance_page_is_404(client: TestClient):
    assert client.get("/instance/ghost").status_code == 404


def test_api_describes_the_topology(client: TestClient):
    payload = client.get("/api/instances/lab").json()
    assert payload["version"] == "24.8.1.1"
    assert [node["name"] for node in payload["nodes"]] == [
        "clickhouse1", "clickhouse2", "clickhouse3", "clickhouse4",
    ]
    assert payload["nodes"][0]["play"].endswith("/play")
    assert payload["users"] == ["dev"]


def test_api_404_for_unknown_instance(client: TestClient):
    assert client.get("/api/instances/ghost").status_code == 404


def test_create_rejects_an_invalid_name(client: TestClient):
    response = client.post("/api/instances", json={"source": "/bin/clickhouse", "name": "Bad Name"})
    assert response.status_code == 400
    assert "lowercase" in response.json()["detail"]


def test_create_rejects_an_impossible_topology(client: TestClient):
    response = client.post("/api/instances", json={"source": "/x", "name": "z", "shards": 0})
    assert response.status_code == 400


def test_create_returns_a_job_and_a_derived_name(client: TestClient, monkeypatch):
    monkeypatch.setattr(Runtime, "up", lambda self, spec, **kwargs: None)
    payload = client.post(
        "/api/instances", json={"source": "docker://x/y:26.3.12.20001"}
    ).json()
    assert payload["name"] == "ch-26-3-12-20001"
    assert payload["job"]["status"] in {"running", "done"}


def test_job_status_can_be_polled(client: TestClient, monkeypatch):
    monkeypatch.setattr(Runtime, "up", lambda self, spec, **kwargs: None)
    job_id = client.post("/api/instances", json={"source": "/x", "name": "polled"}).json()["job"]["id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["status"] == "done"


def test_failed_job_reports_the_error(client: TestClient, monkeypatch):
    def explode(self, spec, **kwargs):
        raise RuntimeError("no docker for you")

    monkeypatch.setattr(Runtime, "up", explode)
    job_id = client.post("/api/instances", json={"source": "/x", "name": "doomed"}).json()["job"]["id"]
    for _ in range(50):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] != "running":
            break
    assert job["status"] == "failed"
    assert "no docker for you" in job["error"]


def test_unknown_job_is_404(client: TestClient):
    assert client.get("/api/jobs/nope").status_code == 404


def test_query_fans_out_to_every_node(client: TestClient, monkeypatch):
    monkeypatch.setattr(
        Runtime,
        "query_all",
        lambda self, name, sql, **kwargs: [
            QueryResult(columns=["n"], rows=[[1]], node="clickhouse1"),
            QueryError("boom", node="clickhouse2"),
        ],
    )
    results = client.post(
        "/api/instances/lab/query", json={"sql": "SELECT 1", "all_nodes": True}
    ).json()["results"]
    assert results[0]["rows"] == [[1]]
    assert results[1]["error"] == "boom"


def test_empty_query_is_rejected(client: TestClient):
    assert client.post("/api/instances/lab/query", json={"sql": "   "}).status_code == 400


def test_unknown_action_is_rejected(client: TestClient):
    assert client.post("/api/instances/lab/actions/explode").status_code == 400
