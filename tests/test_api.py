"""The healthcheck compose gates on, and the metrics endpoint."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.config import Settings
from core.migrate import MigrationDriftError

pytestmark = pytest.mark.requires_db


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    """App wired to a throwaway database, migrations applied during startup."""
    with TestClient(create_app(settings)) as client:
        yield client


def test_healthz_is_green_once_migrations_are_applied(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "ok"
    assert body["pending_migrations"] == 0
    assert body["schema_version"] >= 1


def test_startup_applies_migrations(client: TestClient) -> None:
    assert client.get("/healthz").json()["schema_version"] >= 1


def test_healthz_is_red_when_migrations_are_pending(settings: Settings) -> None:
    unmigrated = settings.model_copy(update={"migrate_on_startup": False})

    with TestClient(create_app(unmigrated)) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "ok"
    assert body["pending_migrations"] >= 1
    assert "001_schema" in body["detail"]


def test_healthz_is_red_when_the_database_is_unreachable(settings: Settings) -> None:
    unreachable = settings.model_copy(
        update={
            "database_url": "postgresql://127.0.0.1:1/nonexistent?connect_timeout=1",
            "migrate_on_startup": False,
        }
    )

    with TestClient(create_app(unreachable)) as client:
        response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"] == "unreachable"
    assert body["schema_version"] is None


def test_healthz_reports_migration_drift_as_degraded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise MigrationDriftError("001_schema was edited after being applied")

    monkeypatch.setattr("api.main.status", explode)
    response = client.get("/healthz")

    assert response.status_code == 503
    body = response.json()
    assert body["database"] == "ok"
    assert "edited after being applied" in body["detail"]


def test_metrics_exposes_prometheus_text(client: TestClient) -> None:
    response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "hippo_schema_version" in response.text


def test_metrics_counts_requests_by_route(client: TestClient) -> None:
    client.get("/healthz")
    body = client.get("/metrics").text

    assert 'hippo_http_requests_total{method="GET",path="/healthz",status="200"}' in body


def test_openapi_documents_the_healthcheck(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    assert "/healthz" in spec["paths"]
    assert spec["info"]["title"] == "Hippo"
    assert "/metrics" not in spec["paths"], "metrics is not part of the public contract"
