"""FastAPI application backing `chlab ui`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from ..core import profiles as profiles_module
from ..core.query import QueryError, QueryResult
from ..core.registry import InstanceNotFound
from ..core.runtime import InstanceStatus, Runtime
from ..core.spec import ClusterSpec, DatabaseSpec, SpecError, UserSpec, suggest_name
from .jobs import JobRunner

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))


class UserPayload(BaseModel):
    name: str
    password: str = ""
    role: str = "readwrite"
    grants: list[str] = Field(default_factory=list)


class DatabasePayload(BaseModel):
    name: str
    engine: str = "Atomic"


class CreatePayload(BaseModel):
    source: str
    name: str | None = None
    version: str | None = None
    shards: int = 1
    replicas: int = 1
    keeper_nodes: int | None = None
    cluster_name: str = "default_cluster"
    users: list[UserPayload] = Field(default_factory=list)
    databases: list[DatabasePayload] = Field(default_factory=list)
    settings: dict[str, str] = Field(default_factory=dict)
    seed: bool = False
    save_as_profile: str | None = None


class QueryPayload(BaseModel):
    sql: str
    node: str | None = None
    all_nodes: bool = False
    user: str = "default"
    password: str = ""
    database: str | None = None


def create_app(runtime: Runtime | None = None) -> FastAPI:
    app = FastAPI(title="chlab", docs_url=None, redoc_url=None)
    runtime = runtime or Runtime()
    jobs = JobRunner()

    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # --- pages ---------------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={"instances": [_status_dict(status) for status in runtime.list()]},
        )

    @app.get("/new", response_class=HTMLResponse)
    def new(request: Request):
        return templates.TemplateResponse(
            request=request,
            name="new.html",
            context={"profiles": profiles_module.Profiles.default().all()},
        )

    @app.get("/instance/{name}", response_class=HTMLResponse)
    def instance(request: Request, name: str):
        try:
            status = runtime.status(name)
        except InstanceNotFound as failure:
            raise HTTPException(status_code=404, detail=str(failure)) from failure
        return templates.TemplateResponse(
            request=request, name="instance.html", context={"instance": _status_dict(status)}
        )

    # --- api -----------------------------------------------------------------------------

    @app.get("/api/instances")
    def list_instances():
        return [_status_dict(status) for status in runtime.list()]

    @app.get("/api/instances/{name}")
    def get_instance(name: str):
        return _status_dict(_status_or_404(runtime, name))

    @app.post("/api/instances")
    def create_instance(payload: CreatePayload):
        taken = {spec.name for spec in runtime.registry.list()}
        try:
            spec = ClusterSpec(
                name=payload.name or suggest_name(payload.source, taken=taken),
                source=payload.source,
                version=payload.version or None,
                shards=payload.shards,
                replicas=payload.replicas,
                keeper_nodes=payload.keeper_nodes,
                cluster_name=payload.cluster_name or "default_cluster",
                users=tuple(
                    UserSpec(
                        name=user.name.strip(),
                        password=user.password,
                        role=user.role,
                        grants=tuple(grant for grant in user.grants if grant.strip()),
                    )
                    for user in payload.users
                    if user.name.strip()
                ),
                databases=tuple(
                    DatabaseSpec(name=database.name, engine=database.engine)
                    for database in payload.databases
                    if database.name.strip()
                ),
                settings=payload.settings,
                seed=payload.seed,
            )
        except SpecError as failure:
            raise HTTPException(status_code=400, detail=str(failure)) from failure

        if payload.save_as_profile:
            profiles_module.Profiles.default().save(payload.save_as_profile, spec)

        job = jobs.start("up", spec.name, lambda record: runtime.up(spec, on_event=record))
        jobs.prune()
        return {"job": job.to_dict(), "name": spec.name}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="no such job")
        return job.to_dict()

    @app.post("/api/instances/{name}/actions/{action}")
    def act(name: str, action: str):
        actions = {
            "start": runtime.start,
            "stop": runtime.stop,
            "down": runtime.down,
            "destroy": runtime.destroy,
        }
        if action not in actions:
            raise HTTPException(status_code=400, detail=f"unknown action {action!r}")
        _status_or_404(runtime, name)
        job = jobs.start(action, name, lambda _record: actions[action](name))
        return {"job": job.to_dict()}

    @app.post("/api/instances/{name}/query")
    def query(name: str, payload: QueryPayload):
        _status_or_404(runtime, name)
        if not payload.sql.strip():
            raise HTTPException(status_code=400, detail="no SQL given")
        try:
            if payload.all_nodes:
                results = runtime.query_all(
                    name,
                    payload.sql,
                    user=payload.user,
                    password=payload.password,
                    database=payload.database or None,
                )
            else:
                results = [
                    runtime.query(
                        name,
                        payload.sql,
                        node=payload.node,
                        user=payload.user,
                        password=payload.password,
                        database=payload.database or None,
                    )
                ]
        except (QueryError, KeyError) as failure:
            raise HTTPException(status_code=400, detail=str(failure)) from failure
        return {"results": [_result_dict(result) for result in results]}

    @app.get("/api/profiles")
    def list_profiles():
        return profiles_module.Profiles.default().all()

    @app.exception_handler(InstanceNotFound)
    def not_found(_request: Request, failure: InstanceNotFound):
        return JSONResponse(status_code=404, content={"detail": str(failure)})

    return app


def _status_or_404(runtime: Runtime, name: str) -> InstanceStatus:
    try:
        return runtime.status(name)
    except InstanceNotFound as failure:
        raise HTTPException(status_code=404, detail=str(failure)) from failure


def _status_dict(status: InstanceStatus) -> dict[str, Any]:
    spec = status.spec
    return {
        "name": spec.name,
        "state": status.state,
        "version": spec.version,
        "source": spec.source,
        "shards": spec.shards,
        "replicas": spec.replicas,
        "cluster_name": spec.cluster_name,
        "seed": spec.seed,
        "users": [user.name for user in spec.users],
        "databases": [database.name for database in spec.databases],
        "nodes": [
            {
                "name": node.name,
                "shard": node.shard,
                "replica": node.replica,
                "http": node.http_url,
                "play": node.play_url,
                "native": node.tcp_port,
            }
            for node in status.topology.nodes
        ],
        "keepers": [
            {"name": keeper.name, "port": keeper.client_port} for keeper in status.topology.keepers
        ],
    }


def _result_dict(result: QueryResult | QueryError) -> dict[str, Any]:
    if isinstance(result, QueryError):
        return {"node": result.node, "error": str(result)}
    return {
        "node": result.node,
        "columns": result.columns,
        "types": result.types,
        "rows": result.rows,
        "rows_read": result.rows_read,
        "elapsed": result.elapsed,
    }
