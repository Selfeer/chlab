"""Helpers for scripts run with ``chlab py``.

Everything is read from the environment ``chlab py`` sets up, so the same script works
against any instance:

    from chlab_tools import catalog, query, s3

    table = catalog().create_table("demo.events", schema=..., location="s3://warehouse/events")
    table.append(arrow_table)
    print(query("SELECT count() FROM icebergS3(s3, filename = 'events')"))

Only the standard library is imported at module level; pyiceberg and boto3 load on first use.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

#: The first bucket of the s3 service, e.g. ``warehouse``.
BUCKET = os.environ.get("CHLAB_S3_BUCKET", "")
#: Every bucket of the s3 service.
BUCKETS = [bucket for bucket in os.environ.get("CHLAB_S3_BUCKETS", "").split(",") if bucket]
#: pyiceberg catalog names available here, e.g. ``["rest", "ice"]``.
CATALOGS = [name for name in os.environ.get("CHLAB_CATALOGS", "").split(",") if name]
#: ClickHouse node name -> HTTP URL inside the network.
NODES: dict[str, str] = json.loads(os.environ.get("CHLAB_NODES", "{}"))


class QueryError(Exception):
    """A query ClickHouse rejected; ``code`` is its error code when the server sent one."""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def catalog(name: str | None = None):
    """A pyiceberg catalog. Defaults to the first catalog the instance runs."""
    if name is None:
        if not CATALOGS:
            raise RuntimeError("this instance runs no catalog; launch it with --with iceberg-rest")
        name = CATALOGS[0]

    from pyiceberg.catalog import load_catalog

    return load_catalog(name)


def s3():
    """A boto3 S3 client for the s3 service, path-style, as root."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ["CHLAB_S3_ENDPOINT"],
        config=Config(s3={"addressing_style": "path"}),
    )


def query(
    sql: str,
    *,
    node: str | None = None,
    user: str = "default",
    password: str = "",
    settings: dict[str, object] | None = None,
) -> str:
    """Run one statement on a node over HTTP and return the raw response body."""
    target = NODES[node] if node else next(iter(NODES.values()))
    params = urllib.parse.urlencode({key: str(value) for key, value in (settings or {}).items()})
    request = urllib.request.Request(
        f"{target}/?{params}",
        data=sql.encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            return response.read().decode()
    except urllib.error.HTTPError as error:
        code = error.headers.get("X-ClickHouse-Exception-Code")
        raise QueryError(
            error.read().decode().strip(), int(code) if code and code.isdigit() else None
        ) from None


def rows(sql: str, **kwargs) -> list[dict]:
    """Run a query and return its rows as dicts."""
    body = query(f"{sql.rstrip().rstrip(';')} FORMAT JSONEachRow", **kwargs)
    return [json.loads(line) for line in body.splitlines() if line.strip()]
