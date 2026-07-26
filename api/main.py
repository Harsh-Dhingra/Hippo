"""The API process.

Two operational endpoints — a healthcheck `docker compose up` can gate on, and
Prometheus metrics — plus the v1 surface in api/routes.py.

The agent is built on first use rather than at startup. Its connector directory
needs a database, and building the app must not require one: an unreachable
database is something /healthz reports, not something that stops the process.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager, suppress
from typing import Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest
from pydantic import BaseModel, Field

from agent.links import load_directory
from agent.loop import Agent
from agent.policy import load_policy
from agent.providers import build_provider
from api.approvals import auto_approve_pending
from api.routes import build_router
from core.alerts import notify
from core.config import Settings, get_settings
from core.db import Connection, Database, connect
from core.logging import configure_logging
from core.metrics import DatabaseCollector
from core.migrate import MigrationError, status, upgrade
from resolver.embeddings import build_provider as build_embeddings

LOG = logging.getLogger("hippo.api")

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


async def _sweep(db: Database, settings: Settings) -> None:
    """Apply the operator's policy to whatever is waiting.

    Silent and cheap when auto-approval is off, which is the default: the
    policy is checked before any query runs. Failures are logged and the loop
    continues — a governance sweep that died on one bad tick would stop
    approving without anyone noticing, which is worse than the tick failing.
    """
    policy = load_policy(settings.risk_policy_path)
    if not policy.auto_approve.enabled:
        LOG.info("auto-approval is off; every action waits for a person")
        return

    LOG.warning(
        "auto-approval is on",
        extra={
            "action_types": sorted(policy.auto_approve.action_types),
            "max_per_hour": policy.auto_approve.max_per_hour,
        },
    )
    while True:
        await asyncio.sleep(settings.auto_approve_interval_seconds)
        try:
            with db.connection(timeout=5.0) as conn:
                approved = auto_approve_pending(conn, policy)
            if approved:
                LOG.info("policy approved actions", extra={"count": len(approved)})
        except Exception as exc:
            LOG.error("auto-approval sweep failed", extra={"error": str(exc)})


async def _deliver_alerts(db: Database, settings: Settings) -> None:
    """Push open alerts to the configured webhook.

    Silent when no webhook is configured, which is the default: alerts are
    still recorded and shown in the UI, and delivery is the opt-in part. The
    loop survives its own failures for the same reason the sweep does — a
    notifier that died on one bad tick would stop notifying without anyone
    noticing, which is the exact failure it exists to prevent.
    """
    webhook = settings.alert_webhook_url.get_secret_value()
    if not webhook:
        return

    LOG.info("alert webhook configured")
    while True:
        await asyncio.sleep(settings.alert_interval_seconds)
        try:
            with db.connection(timeout=5.0) as conn:
                sent = notify(conn, webhook)
            if sent:
                LOG.info("alerts delivered", extra={"count": sent})
        except Exception as exc:
            LOG.error("alert delivery failed", extra={"error": str(exc)})


class _LazyDatabase:
    """Defers to app.state.db, which the lifespan sets.

    The router needs somewhere to get a connection from, and the pool is opened
    at startup rather than at import. This is that indirection and nothing more.
    """

    def __init__(self, app: FastAPI) -> None:
        self._app = app

    def connection(self, timeout: float | None = None) -> AbstractContextManager[Connection]:
        db: Database = self._app.state.db
        return db.connection(timeout=timeout)


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
        # State metrics — sync lag, queue depth, tokens spent — are read from
        # the database when /metrics is scraped. A counter in this process
        # could not report them: they outlive it, and a freshly booted process
        # knows none of its own history.
        collector = DatabaseCollector(db)
        REGISTRY.register(collector)

        # Auto-approval runs here rather than in the sync worker. The worker
        # holds the credentials and does the executing; letting it also approve
        # would collapse propose, approve and execute into one component. This
        # process represents people and holds no source-system credential, so
        # what decides still cannot be what acts.
        sweeper = asyncio.create_task(_sweep(db, resolved))
        alerter = asyncio.create_task(_deliver_alerts(db, resolved))
        try:
            yield
        finally:
            for task in (sweeper, alerter):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            REGISTRY.unregister(collector)
            db.close()

    app = FastAPI(
        title="Hippo",
        version="0.1.0",
        summary="Open-source enterprise memory.",
        description=(
            "Every answer is filtered by the permissions of the person asking, "
            "enforced in the database rather than in application code. Actions "
            "are proposed and wait for a human."
        ),
        lifespan=lifespan,
    )

    cached: dict[str, Agent] = {}

    def agent(conn: Connection) -> Agent:
        if "agent" not in cached:
            cached["agent"] = Agent(
                build_provider(resolved),
                embedder=build_embeddings(resolved),
                directory=load_directory(conn),
                policy=load_policy(resolved.risk_policy_path),
            )
        return cached["agent"]

    # The router borrows connections through the pool the lifespan opens, so it
    # reads app.state.db at request time rather than capturing a pool that does
    # not exist yet.
    app.include_router(build_router(_LazyDatabase(app), agent, build_embeddings(resolved)))

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
