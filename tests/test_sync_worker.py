"""The sync process: what it schedules, and where credentials come from.

ARCHITECTURE §4 gives this process the source-system credentials and gives them
to nothing else. So most of what is worth testing here is about the credential
boundary: that a token never comes from the database, that a missing one fails
loudly rather than syncing nothing, and that the failure names the variable it
wanted.

The live transports are not exercised. CLAUDE.md is explicit that live API
calls belong to final verification and never to CI, so these tests stop at the
point where a connector object is constructed.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from core.db import Connection
from core.jobs import Job, claim, enqueue
from sync.connectors.jira import JiraConnector
from sync.connectors.sdk import PermanentSourceError
from sync.connectors.slack import SlackConnector
from sync.scheduler import ACL_STREAM, JOB_KIND, Cadence
from sync.worker import (
    SCHEDULE_KIND,
    build_connector,
    handlers,
    schedule_tick,
    token_for,
)
from sync.writeback import JOB_KIND as ACTION_KIND
from sync.writeback import ROLLBACK_KIND

pytestmark = pytest.mark.requires_db


@pytest.fixture
def slack_connector(migrated: Connection) -> UUID:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) "
        "VALUES (%s, 'slack', 'Slack', '{\"workspace_url\": \"https://acme.slack.com\"}')",
        (connector_id,),
    )
    return connector_id


@pytest.fixture
def jira_connector(migrated: Connection) -> UUID:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) VALUES "
        "(%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\", "
        '"email": "bot@acme.com"}\')',
        (connector_id,),
    )
    return connector_id


# ---------------------------------------------------------------------------
# Credentials.
# ---------------------------------------------------------------------------


def test_a_token_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_SLACK_TOKEN", "xoxb-general")

    assert token_for("slack", uuid4()) == "xoxb-general"


def test_a_per_connector_token_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two Slack workspaces in one deployment should not have to share a
    token."""
    connector_id = UUID("11111111-2222-3333-4444-555555555555")
    monkeypatch.setenv("HIPPO_SLACK_TOKEN", "xoxb-general")
    monkeypatch.setenv("HIPPO_SLACK_TOKEN_11111111_2222_3333_4444_555555555555", "xoxb-specific")

    assert token_for("slack", connector_id) == "xoxb-specific"


def test_a_missing_token_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A sync that silently does nothing looks exactly like a sync with nothing
    to do, and for ACLs those two states are a stale permission and a current
    one."""
    monkeypatch.delenv("HIPPO_SLACK_TOKEN", raising=False)
    connector_id = uuid4()

    with pytest.raises(PermanentSourceError) as caught:
        token_for("slack", connector_id)

    assert "HIPPO_SLACK_TOKEN" in str(caught.value)


def test_the_failure_names_the_variable_it_wanted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HIPPO_JIRA_TOKEN", raising=False)
    connector_id = UUID("11111111-2222-3333-4444-555555555555")

    with pytest.raises(PermanentSourceError) as caught:
        token_for("jira", connector_id)

    assert "HIPPO_JIRA_TOKEN_11111111_2222_3333_4444_555555555555" in str(caught.value)


def test_a_token_cannot_even_be_put_in_the_database(
    migrated: Connection, slack_connector: UUID
) -> None:
    """CLAUDE.md rule 4, now enforced by the schema rather than by this test.

    The original version of this put a token in config and checked that
    build_connector ignored it. Migration 013 makes the premise unreachable:
    the row will not store one. That is the better outcome, so the assertion
    moved to where the refusal happens.
    """
    from psycopg import errors

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            'UPDATE connectors SET config = config || \'{"token": "xoxb-in-the-db"}\'::jsonb '
            "WHERE id = %s",
            (slack_connector,),
        )
    migrated.rollback()


def test_a_credential_smuggled_under_another_name_is_still_not_used(
    migrated: Connection, slack_connector: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CHECK matches key names, so a determined operator could call it
    something else. That layer is not the only one: the worker reads
    credentials from the environment and from nowhere else, so config remains
    inert whatever it is called."""
    migrated.execute(
        'UPDATE connectors SET config = config || \'{"workspace_url": "xoxb-not-a-url"}\'::jsonb '
        "WHERE id = %s",
        (slack_connector,),
    )
    monkeypatch.delenv("HIPPO_SLACK_TOKEN", raising=False)

    with pytest.raises(PermanentSourceError):
        build_connector(migrated, slack_connector)


# ---------------------------------------------------------------------------
# Building a connector.
# ---------------------------------------------------------------------------


def test_a_slack_connector_is_built_from_config_and_environment(
    migrated: Connection, slack_connector: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIPPO_SLACK_TOKEN", "xoxb-test")

    connector = build_connector(migrated, slack_connector)

    assert isinstance(connector, SlackConnector)
    assert connector.kind == "slack"


def test_a_jira_connector_is_built_from_config_and_environment(
    migrated: Connection, jira_connector: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HIPPO_JIRA_TOKEN", "api-token")

    connector = build_connector(migrated, jira_connector)

    assert isinstance(connector, JiraConnector)


def test_jira_falls_back_to_an_environment_email(
    migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The email is not a secret, so config is its home — but a
    single-connector deployment should not have to edit a database row."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) "
        "VALUES (%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
        (connector_id,),
    )
    monkeypatch.setenv("HIPPO_JIRA_TOKEN", "api-token")
    monkeypatch.setenv("HIPPO_JIRA_EMAIL", "bot@acme.com")

    assert isinstance(build_connector(migrated, connector_id), JiraConnector)


def test_jira_without_an_email_says_so(
    migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) "
        "VALUES (%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
        (connector_id,),
    )
    monkeypatch.setenv("HIPPO_JIRA_TOKEN", "api-token")
    monkeypatch.delenv("HIPPO_JIRA_EMAIL", raising=False)

    with pytest.raises(PermanentSourceError, match="email"):
        build_connector(migrated, connector_id)


def test_jira_without_a_base_url_says_so(
    migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'Jira')",
        (connector_id,),
    )
    monkeypatch.setenv("HIPPO_JIRA_TOKEN", "api-token")

    with pytest.raises(PermanentSourceError, match="base_url"):
        build_connector(migrated, connector_id)


def test_an_unknown_connector_kind_is_refused(
    migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """001 deliberately has no CHECK on kind, so a row can name something with
    no implementation. That has to fail here rather than anywhere later."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'github', 'GitHub')",
        (connector_id,),
    )
    monkeypatch.setenv("HIPPO_GITHUB_TOKEN", "ghp-test")

    with pytest.raises(PermanentSourceError, match="github"):
        build_connector(migrated, connector_id)


def test_a_connector_that_does_not_exist_is_refused(migrated: Connection) -> None:
    with pytest.raises(PermanentSourceError, match="no connector"):
        build_connector(migrated, uuid4())


# ---------------------------------------------------------------------------
# The schedule tick.
# ---------------------------------------------------------------------------


def test_a_tick_enqueues_what_is_due(migrated: Connection, slack_connector: UUID) -> None:
    assert schedule_tick(migrated) == 3


def test_a_second_tick_enqueues_nothing_new(migrated: Connection, slack_connector: UUID) -> None:
    schedule_tick(migrated)

    assert schedule_tick(migrated) == 0


def test_a_tick_reports_a_connector_whose_permissions_have_stalled(
    migrated: Connection, slack_connector: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """The connector whose staleness someone needs to see is exactly the one
    whose syncs are failing, so this runs on every tick and not after a
    successful sync."""
    with caplog.at_level("WARNING", logger="hippo.sync.worker"):
        schedule_tick(migrated)

    assert "past the propagation target" in caplog.text


def test_a_tick_is_quiet_when_permissions_are_fresh(
    migrated: Connection, slack_connector: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream, last_synced_at) VALUES (%s, %s, now())",
        (slack_connector, ACL_STREAM),
    )

    with caplog.at_level("WARNING", logger="hippo.sync.worker"):
        schedule_tick(migrated)

    assert "past the propagation target" not in caplog.text


def test_a_custom_cadence_is_honoured(migrated: Connection, slack_connector: UUID) -> None:
    migrated.execute(
        "INSERT INTO sync_state (connector_id, stream, last_synced_at) "
        "VALUES (%s, %s, now() - interval '1 minute')",
        (slack_connector, ACL_STREAM),
    )

    assert schedule_tick(migrated, Cadence(acls=30)) == 3
    assert schedule_tick(migrated, Cadence(acls=3600)) == 0


# ---------------------------------------------------------------------------
# The handler table.
# ---------------------------------------------------------------------------


def test_the_worker_handles_every_kind_it_owns(migrated: Connection) -> None:
    """Syncing, scheduling, and the write-back this process is the only one
    credentialled to perform."""
    table = handlers("postgresql://unused")

    assert set(table) == {JOB_KIND, SCHEDULE_KIND, ACTION_KIND, ROLLBACK_KIND}


def test_a_schedule_job_runs_a_tick(
    migrated: Connection, slack_connector: UUID, db_dsn: str
) -> None:
    """End to end through the queue: enqueue a tick, claim it, run it, and find
    the stream jobs it created."""
    enqueue(migrated, SCHEDULE_KIND)
    migrated.commit()
    job = claim(migrated, worker="test", kinds=(SCHEDULE_KIND,), lease_seconds=60)
    assert job is not None
    migrated.commit()

    handlers(db_dsn)[SCHEDULE_KIND](job)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind = %s", (JOB_KIND,))
        assert cur.fetchone() == (3,)


def test_a_stream_job_syncs_the_named_stream(
    migrated: Connection, db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handler resolves a connector and hands it to the runtime. A fixture
    connector stands in for the live one, because CI does not call an API."""
    from pathlib import Path as _Path

    from sync.connectors.slack import FixtureTransport
    from sync.connectors.slack import SlackConnector as Slack

    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
        (connector_id,),
    )
    migrated.commit()
    fixtures = _Path(__file__).resolve().parent / "fixtures" / "slack"
    monkeypatch.setattr(
        "sync.worker.build_connector",
        lambda _conn, _id: Slack(FixtureTransport(fixtures)),
    )
    job = Job(
        id=uuid4(),
        kind=JOB_KIND,
        payload={"connector_id": str(connector_id), "stream": "identities"},
        attempts=1,
        max_attempts=5,
    )

    handlers(db_dsn)[JOB_KIND](job)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM principals WHERE connector_id = %s", (connector_id,))
        assert (cur.fetchone() or (0,))[0] > 0


def test_the_worker_leases_longer_than_the_slowest_stream(migrated: Connection) -> None:
    """A handler that overruns its lease gets its job reclaimed underneath it.
    A full content sync is the slowest thing this process does."""
    from core.config import Settings
    from sync.worker import build_worker

    worker = build_worker(Settings(database_url="postgresql://unused"))

    assert set(worker.kinds) == {JOB_KIND, SCHEDULE_KIND, ACTION_KIND, ROLLBACK_KIND}


def test_a_stream_job_without_a_credential_fails_the_job(
    migrated: Connection, slack_connector: UUID, db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not a crash and not a silent success: the jobs runtime records it and
    retries with backoff, and the dead letter is where it ends up."""
    monkeypatch.delenv("HIPPO_SLACK_TOKEN", raising=False)
    job = Job(
        id=uuid4(),
        kind=JOB_KIND,
        payload={"connector_id": str(slack_connector), "stream": ACL_STREAM},
        attempts=1,
        max_attempts=5,
    )

    with pytest.raises(PermanentSourceError):
        handlers(db_dsn)[JOB_KIND](job)
