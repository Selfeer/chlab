"""Query execution over the ClickHouse HTTP interface.

Kept dependency-free on purpose: the CLI, the seeding step and the web editor all funnel
through here, and a local tool should not need a driver to run ``SELECT 1``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_TIMEOUT = 60
DEFAULT_FORMAT = "JSONCompact"


class QueryError(RuntimeError):
    """A query the server rejected, carrying the server's own message."""

    def __init__(self, message: str, *, node: str | None = None):
        super().__init__(message)
        self.node = node


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    types: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)
    rows_read: int = 0
    elapsed: float = 0.0
    #: Set when the result came from a fan-out across nodes.
    node: str | None = None

    @property
    def is_empty(self) -> bool:
        return not self.columns and not self.rows


def execute(
    url: str,
    sql: str,
    *,
    user: str = "default",
    password: str = "",
    database: str | None = None,
    settings: dict[str, str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    node: str | None = None,
) -> QueryResult:
    """Run ``sql`` against a node's HTTP port and return its result."""
    params: dict[str, str] = {"default_format": DEFAULT_FORMAT}
    if database:
        params["database"] = database
    params.update(settings or {})

    request = urllib.request.Request(
        f"{url}/?{urllib.parse.urlencode(params)}",
        data=sql.encode(),
        method="POST",
        headers={
            "X-ClickHouse-User": user,
            "X-ClickHouse-Key": password,
            "Content-Type": "text/plain; charset=utf-8",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode(errors="replace")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace").strip()
        raise QueryError(detail or f"HTTP {error.code} {error.reason}", node=node) from error
    except urllib.error.URLError as error:
        raise QueryError(f"could not reach {url}: {error.reason}", node=node) from error
    except TimeoutError as error:
        raise QueryError(f"{url} timed out after {timeout}s", node=node) from error

    return _parse(body, node=node)


def _parse(body: str, *, node: str | None) -> QueryResult:
    text = body.strip()
    if not text:
        # DDL and INSERT return nothing.
        return QueryResult(node=node)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        # A non-JSON body means the query carried its own FORMAT clause; surface it as one cell.
        return QueryResult(columns=["result"], rows=[[body.rstrip("\n")]], node=node)

    meta = payload.get("meta", [])
    statistics = payload.get("statistics", {})
    return QueryResult(
        columns=[column["name"] for column in meta],
        types=[column["type"] for column in meta],
        rows=payload.get("data", []),
        rows_read=statistics.get("rows_read", payload.get("rows", 0)),
        elapsed=statistics.get("elapsed", 0.0),
        node=node,
    )


def keeper_ok(port: int, *, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    """True when Keeper answers the ``ruok`` four-letter word with ``imok``.

    Keeper binds its client port only after the Raft election settles, so a plain TCP connect
    is not enough to know it will serve requests.
    """
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout) as connection:
            connection.sendall(b"ruok")
            return connection.recv(4) == b"imok"
    except OSError:
        return False


def ping(url: str, *, timeout: int = 5) -> bool:
    """True when the node answers its health endpoint."""
    try:
        with urllib.request.urlopen(f"{url}/ping", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False
