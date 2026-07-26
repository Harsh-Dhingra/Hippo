"""P2-OBS-1: metrics an operator can act on, and a dashboard that orders them.

The load-bearing test here is the one where the database is unreachable. These
metrics are what someone looks at *because* something is wrong, so a collector
that raises when its dependency is down takes the monitoring away at exactly
the moment it is needed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from core.db import Connection, Database
from core.metrics import QUERIES, DatabaseCollector

pytestmark = pytest.mark.requires_db

DASHBOARD = Path(__file__).resolve().parents[1] / "deploy" / "grafana" / "hippo-overview.json"


def collect(db: Database) -> dict[str, dict[tuple[str, ...], float]]:
    """Every sample the collector produces, keyed by metric and labels."""
    samples: dict[str, dict[tuple[str, ...], float]] = {}
    for family in DatabaseCollector(db).collect():
        for sample in family.samples:
            samples.setdefault(sample.name, {})[tuple(sample.labels.values())] = sample.value
    return samples


@pytest.fixture
def db(db_dsn: str, migrated: Connection) -> Database:
    pool = Database(db_dsn, min_size=1, max_size=2)
    pool.open()
    return pool


# ---------------------------------------------------------------------------
# The case that matters: the database is gone.
# ---------------------------------------------------------------------------


def test_a_scrape_survives_an_unreachable_database() -> None:
    """A monitoring endpoint that goes down with its dependency goes quiet
    exactly when someone is looking at it."""
    unreachable = Database("postgresql://nobody@127.0.0.1:1/nope", min_size=0, max_size=1)
    unreachable.open(wait=False)

    samples = collect(unreachable)

    assert samples["hippo_metrics_scrape_failed"][()] == 1.0


def test_a_healthy_scrape_says_so(db: Database) -> None:
    """The panel that says whether to believe the other panels."""
    samples = collect(db)

    assert samples["hippo_metrics_scrape_failed"][()] == 0.0


def test_one_broken_query_does_not_cost_the_others(
    db: Database, migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A table a migration has not created yet must not blank the dashboard.

    The broken query goes *first*, which is the case that needs the rollback: a
    failed statement aborts the transaction, so without one every query after
    it fails too and one missing table costs every panel.
    """
    from core.jobs import enqueue

    enqueue(migrated, "sync.stream", dedupe_key="one")
    migrated.commit()

    broken = {
        "hippo_nonsense": ("deliberately invalid", "SELECT * FROM no_such_table", ()),
        **QUERIES,
    }
    monkeypatch.setattr("core.metrics.QUERIES", broken)

    samples = collect(db)

    assert "hippo_nonsense" not in samples
    assert samples["hippo_metrics_scrape_failed"][()] == 0.0
    assert samples["hippo_jobs_queue_depth"][("sync.stream", "pending")] == 1.0


# ---------------------------------------------------------------------------
# What it reports.
# ---------------------------------------------------------------------------


def test_queue_depth_counts_by_kind_and_status(db: Database, migrated: Connection) -> None:
    from core.jobs import enqueue

    enqueue(migrated, "sync.stream", {"a": 1}, dedupe_key="one")
    enqueue(migrated, "sync.stream", {"a": 2}, dedupe_key="two")
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_jobs_queue_depth"][("sync.stream", "pending")] == 2.0


def test_the_oldest_runnable_job_is_reported(db: Database, migrated: Connection) -> None:
    """The clearest single signal that no worker is running."""
    from core.jobs import enqueue

    enqueue(migrated, "sync.stream", dedupe_key="old")
    migrated.execute("UPDATE jobs SET run_at = now() - interval '10 minutes'")
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_jobs_oldest_pending_seconds"][("sync.stream",)] > 500


def test_a_job_scheduled_for_later_is_not_counted_as_late(
    db: Database, migrated: Connection
) -> None:
    """Backoff is not lateness. Counting a retry's delay as queue age would
    make every transient failure look like an outage."""
    from core.jobs import enqueue

    enqueue(migrated, "sync.stream", dedupe_key="future")
    migrated.execute("UPDATE jobs SET run_at = now() + interval '1 hour'")
    migrated.commit()

    samples = collect(db)

    assert ("sync.stream",) not in samples.get("hippo_jobs_oldest_pending_seconds", {})


def test_sync_lag_is_reported_per_stream(db: Database, migrated: Connection) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream, last_synced_at) "
        "VALUES (%s, 'acls', now() - interval '2 minutes'), "
        "       (%s, 'content', now() - interval '20 minutes')",
        (connector_id, connector_id),
    )
    migrated.commit()

    samples = collect(db)

    assert 100 < samples["hippo_sync_lag_seconds"][("slack", "acls")] < 200
    assert samples["hippo_sync_lag_seconds"][("slack", "content")] > 1000


def test_a_never_synced_stream_reports_no_lag(db: Database, migrated: Connection) -> None:
    """Absent rather than zero. Zero is what "just synced" looks like, and a
    missing measurement must not read as the best possible one."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream) VALUES (%s, 'acls')", (connector_id,)
    )
    migrated.commit()

    samples = collect(db)

    assert ("slack", "acls") not in samples.get("hippo_sync_lag_seconds", {})
    assert samples["hippo_sync_stream_failed"][("slack", "acls")] == 0.0


def test_a_failing_stream_is_flagged(db: Database, migrated: Connection) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')",
        (connector_id,),
    )
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream, last_error) VALUES (%s, 'acls', 'HTTP 401')",
        (connector_id,),
    )
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_sync_stream_failed"][("jira", "acls")] == 1.0


def test_token_spend_survives_a_restart(db: Database, migrated: Connection) -> None:
    """Read from the trace log rather than a counter. A process that has just
    booted knows none of its own history."""
    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO query_traces (principal_id, question, plan, route, provider, model, "
        "    input_tokens, output_tokens) "
        "VALUES (%s, 'q', '{}', 'synthesize', 'anthropic', 'claude-opus-5', 1000, 250), "
        "       (%s, 'q2', '{}', 'synthesize', 'anthropic', 'claude-opus-5', 500, 125)",
        (principal, principal),
    )
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_tokens_input_total"][("anthropic", "claude-opus-5")] == 1500.0
    assert samples["hippo_tokens_output_total"][("anthropic", "claude-opus-5")] == 375.0


def test_input_and_output_are_separate_series(db: Database) -> None:
    """They are priced differently, so one total is a number nobody can turn
    into money. Asserted on the families rather than the samples: an empty
    family is correct when nothing has been spent yet."""
    names = {family.name for family in DatabaseCollector(db).collect()}

    assert "hippo_tokens_input_total" in names
    assert "hippo_tokens_output_total" in names


def test_queries_are_counted_by_route(db: Database, migrated: Connection) -> None:
    principal = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO query_traces (principal_id, question, plan, route) VALUES "
        "(%s, 'a', '{}', 'synthesize'), (%s, 'b', '{}', 'nothing_visible'), "
        "(%s, 'c', '{}', 'error')",
        (principal, principal, principal),
    )
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_queries_total"][("error",)] == 1.0
    assert samples["hippo_queries_total"][("nothing_visible",)] == 1.0


def test_actions_are_counted_by_status(db: Database, migrated: Connection) -> None:
    principal, connector, entity = uuid4(), uuid4(), uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')", (connector,)
    )
    migrated.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'ticket')", (entity,))
    migrated.execute(
        "INSERT INTO actions (requested_by, connector_id, action_type, target_entity, payload, "
        "    risk_class) VALUES (%s, %s, 'jira.comment', %s, '{}', 'consequential')",
        (principal, connector, entity),
    )
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_actions_total"][("jira.comment", "pending")] == 1.0


def test_an_action_stuck_mid_write_is_visible(db: Database, migrated: Connection) -> None:
    """Rises only when an executor died between capturing an inverse and
    finishing, which is the state nothing else in the system reports."""
    principal, connector, entity = uuid4(), uuid4(), uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (principal,))
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')", (connector,)
    )
    migrated.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'ticket')", (entity,))
    migrated.execute(
        "INSERT INTO actions (requested_by, connector_id, action_type, target_entity, payload, "
        "    risk_class, status, approved_by, execution_started_at) "
        "VALUES (%s, %s, 'jira.comment', %s, '{}', 'consequential', 'executing', %s, "
        "        now() - interval '30 minutes')",
        (principal, connector, entity, principal),
    )
    migrated.commit()

    samples = collect(db)

    assert samples["hippo_actions_in_flight_seconds"][()] > 1500


# ---------------------------------------------------------------------------
# The endpoint.
# ---------------------------------------------------------------------------


def test_the_metrics_endpoint_serves_the_database_metrics(settings: Any) -> None:
    from fastapi.testclient import TestClient

    from api.main import create_app

    with TestClient(create_app(settings)) as client:
        body = client.get("/metrics").text

    assert "hippo_jobs_queue_depth" in body
    assert "hippo_metrics_scrape_failed" in body
    assert "hippo_sync_lag_seconds" in body


def test_the_collector_is_unregistered_on_shutdown(settings: Any) -> None:
    """Two app instances in one process — which is every test run — would
    otherwise collide on the registry."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    for _ in range(2):
        with TestClient(create_app(settings)) as client:
            assert client.get("/metrics").status_code == 200


# ---------------------------------------------------------------------------
# The dashboard.
# ---------------------------------------------------------------------------


def test_the_dashboard_is_valid_json_grafana_will_import() -> None:
    dashboard = json.loads(DASHBOARD.read_text())

    assert dashboard["uid"] == "hippo-overview"
    assert dashboard["schemaVersion"] >= 39
    assert dashboard["panels"]


def test_every_panel_query_names_a_metric_that_exists() -> None:
    """A dashboard referring to a metric nothing emits is a dashboard of empty
    panels, and empty panels get read as 'nothing is wrong'."""
    emitted = set(QUERIES) | {
        "hippo_tokens_input_total",
        "hippo_tokens_output_total",
        "hippo_metrics_scrape_failed",
        "hippo_acl_staleness_seconds",
    }
    dashboard = json.loads(DASHBOARD.read_text())

    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            expression = target["expr"]
            if expression.startswith("vector("):
                continue
            assert any(metric in expression for metric in emitted), expression


def test_every_panel_says_what_it_means() -> None:
    """A panel whose title is its only explanation is a panel nobody can act
    on at 3am."""
    dashboard = json.loads(DASHBOARD.read_text())

    for panel in dashboard["panels"]:
        if panel["type"] == "row":
            continue
        assert len(panel.get("description", "")) > 40, panel["title"]


def test_the_dashboard_leads_with_the_security_story() -> None:
    """ARCHITECTURE §11 puts ACL propagation under the security story rather
    than under performance. The dashboard should agree."""
    dashboard = json.loads(DASHBOARD.read_text())
    rows = [panel for panel in dashboard["panels"] if panel["type"] == "row"]

    assert rows[0]["title"] == "The security story"
    first_real = next(p for p in dashboard["panels"] if p["type"] != "row")
    assert "acl" in json.dumps(first_real["targets"]).lower()


def test_the_alert_conditions_are_written_down() -> None:
    """Thresholds depend on how often connectors are allowed to run, so they
    are not baked into the JSON — but the conditions worth paging on should not
    be left as folklore."""
    readme = (DASHBOARD.parent / "README.md").read_text()

    for condition in (
        "hippo_acl_staleness_seconds",
        "hippo_jobs_queue_depth",
        "hippo_actions_in_flight_seconds",
        "hippo_sync_stream_failed",
    ):
        assert condition in readme
