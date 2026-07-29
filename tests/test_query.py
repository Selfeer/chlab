"""Query-layer parsing and error handling, against a stub HTTP server."""

from __future__ import annotations

import http.server
import json
import threading

import pytest

from chlab.core.query import QueryError, execute, ping


class _Handler(http.server.BaseHTTPRequestHandler):
    #: Set per test: (status, body).
    response = (200, "")

    def do_POST(self):
        status, body = self.response
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        self.do_POST()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    handler = type("Handler", (_Handler,), {})
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_parses_a_jsoncompact_result(server):
    handler, url = server
    handler.response = (
        200,
        json.dumps(
            {
                "meta": [{"name": "n", "type": "UInt64"}, {"name": "s", "type": "String"}],
                "data": [[1, "a"], [2, "b"]],
                "statistics": {"rows_read": 2, "elapsed": 0.25},
            }
        ),
    )
    result = execute(url, "SELECT 1", node="clickhouse1")
    assert result.columns == ["n", "s"]
    assert result.types == ["UInt64", "String"]
    assert result.rows == [[1, "a"], [2, "b"]]
    assert result.rows_read == 2
    assert result.elapsed == 0.25
    assert result.node == "clickhouse1"


def test_empty_body_means_a_statement_with_no_output(server):
    handler, url = server
    handler.response = (200, "")
    assert execute(url, "CREATE DATABASE x").is_empty


def test_non_json_body_is_surfaced_as_one_cell(server):
    """A query carrying its own FORMAT clause still has to show something."""
    handler, url = server
    handler.response = (200, "1\t2\n")
    result = execute(url, "SELECT 1, 2 FORMAT TSV")
    assert result.columns == ["result"] and result.rows == [["1\t2"]]


def test_server_errors_keep_the_server_message(server):
    handler, url = server
    handler.response = (500, "Code: 62. DB::Exception: Syntax error")
    with pytest.raises(QueryError, match="Syntax error") as failure:
        execute(url, "SELEKT 1", node="clickhouse2")
    assert failure.value.node == "clickhouse2"


def test_unreachable_node_is_a_query_error():
    with pytest.raises(QueryError, match="could not reach"):
        execute("http://127.0.0.1:1", "SELECT 1")


def test_ping_is_false_when_nothing_is_listening():
    assert ping("http://127.0.0.1:1") is False


def test_ping_is_true_when_the_server_answers(server):
    handler, url = server
    handler.response = (200, "Ok.\n")
    assert ping(url) is True
