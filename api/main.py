"""The API process.

v0 surface is deliberately two endpoints: a healthcheck that `docker compose up`
can gate on, and Prometheus metrics. Query, actions and trace endpoints arrive
with P1-SRF-1.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field

from core.config import Settings, get_settings
from core.db import Database, connect
from core.logging import configure_logging
from core.migrate import MigrationError, status, upgrade

REQUESTS = Counter(
    "hippo_http_requests_total",
    "HTTP requests handled by the API.",
    labelnames=("method", "path", "status"),
)
SCHEMA_VERSION = Gauge(
    "hippo_schema_version",
    "Highest applied migration version. -1 when the database is unreachable.",
)


class HealthResponse(BaseModel):
    """Body of GET /healthz."""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable"]
    schema_version: int | None = Field(
        default=None, description="Highest applied migration version."
    )
    pending_migrations: int | None = Field(
        default=None, description="Migrations in the repo that the database has not applied."
    )
    detail: str | None = Field(default=None, description="Why the service is degraded.")


def _check_health(db: Database) -> tuple[HealthResponse, int]:
    """Report on the one dependency this process has.

    Degraded, not healthy, when migrations are pending: a database whose schema
    predates the code is exactly the state a rollout must not be marked green in.
    """
    try:
        with db.connection(timeout=2.0) as conn:
            current = status(conn)
    except MigrationError as exc:
        SCHEMA_VERSION.set(-1)
        return HealthResponse(status="degraded", database="ok", detail=str(exc)), 503
    except Exception as exc:
        SCHEMA_VERSION.set(-1)
        return HealthResponse(status="degraded", database="unreachable", detail=str(exc)), 503

    SCHEMA_VERSION.set(current.current_version if current.current_version is not None else -1)
    if not current.up_to_date:
        pending_names = ", ".join(f"{m.version:03d}_{m.name}" for m in current.pending)
        return HealthResponse(
            status="degraded",
            database="ok",
            schema_version=current.current_version,
            pending_migrations=len(current.pending),
            detail=f"pending migrations: {pending_names}",
        ), 503

    return HealthResponse(
        status="ok",
        database="ok",
        schema_version=current.current_version,
        pending_migrations=0,
    ), 200


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app. Takes settings explicitly so tests need no environment."""
    resolved = settings if settings is not None else get_settings()
    configure_logging(level=resolved.log_level, service=resolved.service_name)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if resolved.migrate_on_startup:
            # Loud on purpose: serving traffic against a half-migrated schema is
            # worse than not starting.
            with connect(resolved.database_url, autocommit=True) as conn:
                upgrade(conn)
        db = Database(
            resolved.database_url,
            min_size=resolved.pool_min_size,
            max_size=resolved.pool_max_size,
        )
        db.open(timeout=resolved.pool_open_timeout, wait=False)
        app.state.db = db
        try:
            yield
        finally:
            db.close()

    app = FastAPI(
        title="Hippo",
        version="0.0.0",
        summary="Open-source enterprise memory.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def _count_requests(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", request.url.path)
        REQUESTS.labels(method=request.method, path=path, status=str(response.status_code)).inc()
        return response

    @app.get("/healthz", response_model=HealthResponse, summary="Liveness and schema state")
    def healthz(request: Request) -> Response:
        db: Database = request.app.state.db
        body, code = _check_health(db)
        return JSONResponse(status_code=code, content=body.model_dump())

    @app.get("/metrics", summary="Prometheus metrics", include_in_schema=False)
    def metrics() -> Response:
        return PlainTextResponse(
            content=generate_latest().decode("utf-8"),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app
