"""P2-OBS-2: making the two silent failures audible.

Schema drift and sync failure both degrade without breaking. A drifted
connector keeps working and extracts slightly less; a failing stream looks,
from every other surface Hippo had, exactly like a stream with nothing to do.
For the ACL stream those two states are a stale permission and a current one.

The tests that carry weight are about noise and about blast radius. An alerting
system that files three hundred and sixty identical rows a day gets ignored, and
one that can take down the sync it is watching is worse than none.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from core.alerts import (
    SCHEMA_DRIFT,
    SYNC_FAILURE,
    acknowledge,
    notify,
    open_alerts,
    raise_alert,
    summarise,
)
from core.db import Connection
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def connector(migrated: Connection) -> UUID:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Acme Slack')",
        (connector_id,),
    )
    return connector_id


@pytest.fixture
def person(migrated: Connection, connector: UUID) -> UUID:
    principal = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, connector_id, source_id) "
        "VALUES (%s, 'user', %s, 'U-OPS')",
        (principal, connector),
    )
    return principal


def responder(status: int = 200) -> Any:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen.append(json.loads(request.content))
        return httpx.Response(status, json={})

    return handler, seen


# ---------------------------------------------------------------------------
# Deduplication, because noise is the failure mode of alerting.
# ---------------------------------------------------------------------------


def test_the_same_problem_is_one_alert(migrated: Connection, connector: UUID) -> None:
    """The ACL stream runs every four minutes. Three hundred and sixty
    identical rows a day is a list nobody reads."""
    for _ in range(5):
        raise_alert(
            migrated,
            SYNC_FAILURE,
            "HTTP 401",
            connector_id=connector,
            stream="acls",
            fingerprint="PermanentSourceError",
        )

    alerts = open_alerts(migrated)

    assert len(alerts) == 1
    assert alerts[0].occurrences == 5
    assert alerts[0].is_recurring is True


def test_a_different_problem_is_a_different_alert(migrated: Connection, connector: UUID) -> None:
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    raise_alert(migrated, SYNC_FAILURE, "HTTP 500", connector_id=connector, stream="acls")

    assert len(open_alerts(migrated)) == 2


def test_the_same_problem_on_another_stream_is_separate(
    migrated: Connection, connector: UUID
) -> None:
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="content")

    assert len(open_alerts(migrated)) == 2


def test_the_detail_is_refreshed_on_a_repeat(migrated: Connection, connector: UUID) -> None:
    """The fingerprint decides identity; the detail should still show the most
    recent wording."""
    raise_alert(
        migrated,
        SYNC_FAILURE,
        "rate limited, retry in 30",
        connector_id=connector,
        stream="acls",
        fingerprint="RateLimitedError",
    )
    raise_alert(
        migrated,
        SYNC_FAILURE,
        "rate limited, retry in 90",
        connector_id=connector,
        stream="acls",
        fingerprint="RateLimitedError",
    )

    assert open_alerts(migrated)[0].detail.endswith("90")


# ---------------------------------------------------------------------------
# Acknowledgement.
# ---------------------------------------------------------------------------


def test_acknowledging_clears_it(migrated: Connection, connector: UUID, person: UUID) -> None:
    alert = raise_alert(migrated, SCHEMA_DRIFT, "v1 -> v2", connector_id=connector)
    assert alert is not None

    assert acknowledge(migrated, alert, person) is True
    assert open_alerts(migrated) == []


def test_acknowledging_twice_is_harmless(
    migrated: Connection, connector: UUID, person: UUID
) -> None:
    alert = raise_alert(migrated, SCHEMA_DRIFT, "v1 -> v2", connector_id=connector)
    assert alert is not None
    acknowledge(migrated, alert, person)

    assert acknowledge(migrated, alert, person) is False


def test_a_cleared_problem_that_comes_back_is_raised_again(
    migrated: Connection, connector: UUID, person: UUID
) -> None:
    """ "We fixed it and it came back" is different news from "it is still
    broken", so it gets a fresh row rather than bumping the old counter."""
    first = raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    assert first is not None
    acknowledge(migrated, first, person)

    second = raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")

    assert second != first
    assert len(open_alerts(migrated)) == 1


def test_an_acknowledgement_records_who(
    migrated: Connection, connector: UUID, person: UUID
) -> None:
    alert = raise_alert(migrated, SCHEMA_DRIFT, "v1 -> v2", connector_id=connector)
    assert alert is not None
    acknowledge(migrated, alert, person)

    with migrated.cursor() as cur:
        cur.execute("SELECT acknowledged_by FROM alerts WHERE id = %s", (alert,))
        assert cur.fetchone() == (person,)


# ---------------------------------------------------------------------------
# The two failures, raised from where they actually happen.
# ---------------------------------------------------------------------------


def test_schema_drift_raises_an_alert(migrated: Connection, connector: UUID) -> None:
    """Previously a log line and nothing else."""
    runtime = SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), connector)
    runtime.sync_stream(migrated, "identities")
    migrated.execute(
        "UPDATE sync_state SET schema_version = 'ancient' WHERE connector_id = %s", (connector,)
    )

    runtime.sync_stream(migrated, "identities")

    drift = [alert for alert in open_alerts(migrated) if alert.kind == SCHEMA_DRIFT]
    assert len(drift) == 1
    assert "ancient" in drift[0].detail


def test_drift_alerts_once_per_transition(migrated: Connection, connector: UUID) -> None:
    """A drifted connector syncs every few minutes and the news does not
    change."""
    runtime = SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), connector)
    runtime.sync_stream(migrated, "identities")
    migrated.execute(
        "UPDATE sync_state SET schema_version = 'ancient' WHERE connector_id = %s", (connector,)
    )
    runtime.sync_stream(migrated, "identities")
    migrated.execute(
        "UPDATE sync_state SET schema_version = 'ancient' WHERE connector_id = %s", (connector,)
    )
    runtime.sync_stream(migrated, "identities")

    drift = [alert for alert in open_alerts(migrated) if alert.kind == SCHEMA_DRIFT]
    assert len(drift) == 1


def test_a_failing_stream_records_its_error(migrated: Connection, connector: UUID) -> None:
    """sync_state.last_error has existed since 001 and nothing ever wrote it."""

    class Broken(SlackConnector):
        def identities(self, cursor: Any) -> Any:
            raise RuntimeError("the workspace is gone")

    with pytest.raises(RuntimeError):
        SyncRuntime(Broken(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
            migrated, "identities"
        )

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT last_error FROM sync_state WHERE connector_id = %s AND stream = 'identities'",
            (connector,),
        )
        row = cur.fetchone()

    assert row is not None
    assert "the workspace is gone" in str(row[0])


def test_a_failing_stream_raises_an_alert(migrated: Connection, connector: UUID) -> None:
    class Broken(SlackConnector):
        def identities(self, cursor: Any) -> Any:
            raise RuntimeError("the workspace is gone")

    with pytest.raises(RuntimeError):
        SyncRuntime(Broken(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
            migrated, "identities"
        )

    failures = [alert for alert in open_alerts(migrated) if alert.kind == SYNC_FAILURE]
    assert len(failures) == 1
    assert failures[0].stream == "identities"


def test_the_failure_still_propagates(migrated: Connection, connector: UUID) -> None:
    """Recorded and re-raised. The jobs runtime owns retry and the dead letter,
    and swallowing the error would turn a failed sync into a successful one."""

    class Broken(SlackConnector):
        def identities(self, cursor: Any) -> Any:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        SyncRuntime(Broken(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
            migrated, "identities"
        )


def test_a_successful_run_clears_the_error(migrated: Connection, connector: UUID) -> None:
    """An operator who learns to ignore a stale red light has lost the
    alerting."""
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream, last_error) "
        "VALUES (%s, 'identities', 'an old failure')",
        (connector,),
    )

    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
        migrated, "identities"
    )

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT last_error FROM sync_state WHERE connector_id = %s AND stream = 'identities'",
            (connector,),
        )
        assert cur.fetchone() == (None,)


def test_recording_a_failure_never_becomes_a_second_failure(
    migrated: Connection, connector: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure to record an alarm must not replace the error the caller was
    already reporting."""

    def exploding(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the alert table is on fire")

    monkeypatch.setattr("sync.runtime.raise_alert", exploding)

    class Broken(SlackConnector):
        def identities(self, cursor: Any) -> Any:
            raise ValueError("the original problem")

    with pytest.raises(ValueError, match="the original problem"):
        SyncRuntime(Broken(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
            migrated, "identities"
        )


# ---------------------------------------------------------------------------
# The webhook. It must never break the thing it watches.
# ---------------------------------------------------------------------------


def test_an_open_alert_is_delivered(migrated: Connection, connector: UUID) -> None:
    handler, seen = responder()
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")

    sent = notify(
        migrated,
        "https://hooks.example.com/x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert sent == 1
    assert seen[0]["kind"] == SYNC_FAILURE
    assert "HTTP 401" in seen[0]["text"]


def test_it_is_not_delivered_twice(migrated: Connection, connector: UUID) -> None:
    """A failing stream would otherwise deliver three hundred and sixty
    messages a day and be muted within the hour."""
    handler, seen = responder()
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    client = httpx.Client(transport=httpx.MockTransport(handler))

    notify(migrated, "https://hooks.example.com/x", client=client)
    notify(migrated, "https://hooks.example.com/x", client=client)

    assert len(seen) == 1


def test_a_failed_delivery_is_retried_next_time(migrated: Connection, connector: UUID) -> None:
    """Marked as notified only on success, so a rotated URL costs a delay
    rather than the alert."""
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    failing, _ = responder(status=500)

    assert (
        notify(
            migrated,
            "https://hooks.example.com/x",
            client=httpx.Client(transport=httpx.MockTransport(failing)),
        )
        == 0
    )

    working, seen = responder()
    assert (
        notify(
            migrated,
            "https://hooks.example.com/x",
            client=httpx.Client(transport=httpx.MockTransport(working)),
        )
        == 1
    )
    assert len(seen) == 1


def test_delivery_never_raises(migrated: Connection, connector: UUID) -> None:
    """A webhook that took down a sync because someone rotated a URL would be
    a monitoring system causing the outage it exists to report."""

    def refusing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")

    assert (
        notify(
            migrated,
            "https://hooks.example.com/x",
            client=httpx.Client(transport=httpx.MockTransport(refusing)),
        )
        == 0
    )


def test_no_webhook_configured_is_not_an_error(migrated: Connection, connector: UUID) -> None:
    """Alerts are still recorded and shown; delivery is the opt-in part."""
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")

    assert notify(migrated, "") == 0
    assert len(open_alerts(migrated)) == 1


def test_an_acknowledged_alert_is_not_delivered(
    migrated: Connection, connector: UUID, person: UUID
) -> None:
    handler, seen = responder()
    alert = raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector)
    assert alert is not None
    acknowledge(migrated, alert, person)

    notify(
        migrated,
        "https://hooks.example.com/x",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert seen == []


def test_the_webhook_url_is_a_secret() -> None:
    """A Slack incoming-webhook URL is a credential: anyone holding it can post
    as the integration."""
    from core.config import Settings

    settings = Settings(
        alert_webhook_url="https://hooks.slack.com/services/T00/B00/xxxxSECRETxxxx",  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )

    assert "xxxxSECRETxxxx" not in f"{settings!r} {settings!s}"
    assert settings.alert_webhook_url.get_secret_value().endswith("xxxxSECRETxxxx")


# ---------------------------------------------------------------------------
# Surfacing.
# ---------------------------------------------------------------------------


def test_the_summary_counts_by_kind(migrated: Connection, connector: UUID) -> None:
    raise_alert(migrated, SYNC_FAILURE, "a", connector_id=connector, stream="acls")
    raise_alert(migrated, SCHEMA_DRIFT, "b", connector_id=connector)

    counts = summarise(migrated)

    assert counts["total"] == 2
    assert counts["by_kind"] == {SYNC_FAILURE: 1, SCHEMA_DRIFT: 1}


def test_metrics_report_open_alerts(migrated: Connection, connector: UUID, db_dsn: str) -> None:
    from core.db import Database
    from core.metrics import DatabaseCollector

    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")
    migrated.commit()
    pool = Database(db_dsn, min_size=1, max_size=2)
    pool.open()

    samples = {
        sample.name: sample.value
        for family in DatabaseCollector(pool).collect()
        for sample in family.samples
    }

    assert samples["hippo_alerts_open"] == 1.0
    assert samples["hippo_alerts_oldest_seconds"] >= 0.0


def test_an_alert_names_where_it_came_from(migrated: Connection, connector: UUID) -> None:
    raise_alert(migrated, SYNC_FAILURE, "HTTP 401", connector_id=connector, stream="acls")

    alert = open_alerts(migrated)[0]

    assert alert.connector == "Acme Slack"
    assert "Acme Slack" in alert.headline
    assert "acls" in alert.headline


def test_a_rate_limited_run_keeps_its_reason(migrated: Connection, connector: UUID) -> None:
    """A rate-limited pass made progress and recorded why it stopped. Clearing
    that on the way out would erase the one explanation for why the stream is
    behind — found by an existing test when the clear was unconditional."""
    from sync.connectors.sdk import RateLimitedError

    class Throttled(SlackConnector):
        def identities(self, cursor: Any) -> Any:
            raise RateLimitedError("slow down", retry_after=30)

    # Not an exception at this level: the runtime treats a rate limit as an
    # incomplete run that will resume, which is why it needs its own guard.
    outcome = SyncRuntime(Throttled(SlackFixtures(FIXTURES / "slack")), connector).sync_stream(
        migrated, "identities"
    )
    assert outcome.complete is False

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT last_error FROM sync_state WHERE connector_id = %s AND stream = 'identities'",
            (connector,),
        )
        row = cur.fetchone()

    assert row is not None
    assert "slow down" in str(row[0])
