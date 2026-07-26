"""Persisting connector records, and the ACL path they feed."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from sync.connectors.sdk import (
    AclRecord,
    ContentRecord,
    Cursor,
    IdentityRecord,
    Page,
    RateLimitedError,
    SourceRef,
)
from sync.connectors.slack import FixtureTransport, SlackConnector
from sync.connectors.slack.connector import CHANNEL
from sync.runtime import (
    SyncRuntime,
    load_cursor,
    persist_acls,
    persist_content,
    persist_identities,
    save_cursor,
)
from tests.mock_connector import MockConnector

pytestmark = pytest.mark.requires_db

SLACK_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slack"


@pytest.fixture
def connector_id(migrated: Connection) -> UUID:
    new_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Test Slack')",
        (new_id,),
    )
    return new_id


@pytest.fixture
def slack_runtime(migrated: Connection, connector_id: UUID) -> SyncRuntime:
    return SyncRuntime(SlackConnector(FixtureTransport(SLACK_FIXTURES)), connector_id)


def principal_id(conn: Connection, connector_id: UUID, source_id: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
            (connector_id, source_id),
        )
        row = cur.fetchone()
    assert row is not None, f"no principal for {source_id}"
    return UUID(str(row[0]))


def scalar(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# Identities.
# ---------------------------------------------------------------------------


def test_identities_become_principals(migrated: Connection, connector_id: UUID) -> None:
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [
            IdentityRecord(
                kind="user", source_id="U-1", email="a@example.com", payload={"id": "U-1"}
            )
        ],
    )

    assert (
        scalar(migrated, "SELECT email FROM principals WHERE source_id = 'U-1'") == "a@example.com"
    )


def test_identities_also_become_raw_records(migrated: Connection, connector_id: UUID) -> None:
    """The resolver builds person entities from these; principals carry only
    the permission story."""
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [IdentityRecord(kind="user", source_id="U-1", payload={"id": "U-1", "tz": "UTC"})],
    )

    assert scalar(migrated, "SELECT payload FROM raw_records WHERE source_type = 'slack.user'") == {
        "id": "U-1",
        "tz": "UTC",
    }


def test_membership_is_recorded(migrated: Connection, connector_id: UUID) -> None:
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [
            IdentityRecord(kind="group", source_id="G-1"),
            IdentityRecord(kind="user", source_id="U-1", member_of=("G-1",)),
        ],
    )

    assert scalar(migrated, "SELECT count(*) FROM principal_memberships") == 1


def test_a_group_referenced_before_it_arrives_is_stubbed(
    migrated: Connection, connector_id: UUID
) -> None:
    """Membership is a fact about the user and must not wait for the group's page."""
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [IdentityRecord(kind="user", source_id="U-1", member_of=("G-LATER",))],
    )

    assert scalar(migrated, "SELECT kind FROM principals WHERE source_id = 'G-LATER'") == "group"
    assert scalar(migrated, "SELECT count(*) FROM principal_memberships") == 1


def test_leaving_a_group_removes_the_membership(migrated: Connection, connector_id: UUID) -> None:
    """A revoked membership arrives as an absence, so only a replace can see it."""
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [IdentityRecord(kind="user", source_id="U-1", member_of=("G-1", "G-2"))],
    )
    assert scalar(migrated, "SELECT count(*) FROM principal_memberships") == 2

    persist_identities(
        migrated,
        "slack",
        connector_id,
        [IdentityRecord(kind="user", source_id="U-1", member_of=("G-1",))],
    )

    assert scalar(migrated, "SELECT count(*) FROM principal_memberships") == 1


def test_resyncing_an_identity_is_idempotent(migrated: Connection, connector_id: UUID) -> None:
    record = IdentityRecord(kind="user", source_id="U-1", email="a@example.com")
    persist_identities(migrated, "slack", connector_id, [record, record])
    persist_identities(migrated, "slack", connector_id, [record])

    assert scalar(migrated, "SELECT count(*) FROM principals") == 1
    assert scalar(migrated, "SELECT count(*) FROM raw_records") == 1


# ---------------------------------------------------------------------------
# Content.
# ---------------------------------------------------------------------------


def test_content_becomes_raw_records_with_their_container(
    migrated: Connection, connector_id: UUID
) -> None:
    persist_content(
        migrated,
        connector_id,
        [
            ContentRecord(
                source_type="slack.message",
                source_id="C-1:1.1",
                payload={"text": "hi"},
                container=SourceRef(source_type=CHANNEL, source_id="C-1"),
            )
        ],
    )

    assert scalar(migrated, "SELECT container_source_id FROM raw_records") == "C-1"


def test_a_resync_updates_rather_than_duplicates(migrated: Connection, connector_id: UUID) -> None:
    """Upsert on (connector, source_type, source_id) is what makes a full
    resync safe."""
    first = ContentRecord(source_type="slack.message", source_id="M-1", payload={"text": "a"})
    edited = ContentRecord(source_type="slack.message", source_id="M-1", payload={"text": "b"})

    persist_content(migrated, connector_id, [first])
    persist_content(migrated, connector_id, [edited])

    assert scalar(migrated, "SELECT count(*) FROM raw_records") == 1
    assert scalar(migrated, "SELECT payload FROM raw_records") == {"text": "b"}


# ---------------------------------------------------------------------------
# ACLs.
# ---------------------------------------------------------------------------


def test_grants_are_stored_against_the_principal(migrated: Connection, connector_id: UUID) -> None:
    persist_identities(
        migrated, "slack", connector_id, [IdentityRecord(kind="user", source_id="U-1")]
    )
    written = persist_acls(
        migrated,
        "slack",
        connector_id,
        [
            AclRecord(
                target=SourceRef(source_type=CHANNEL, source_id="C-1"),
                principal_source_id="U-1",
            )
        ],
    )

    assert written == 1
    assert scalar(migrated, "SELECT target_source_id FROM acl_source_grants") == "C-1"


def test_a_grant_naming_an_unknown_principal_is_dropped(
    migrated: Connection, connector_id: UUID
) -> None:
    """Fail closed. Inventing a principal to hang a grant on fails open."""
    written = persist_acls(
        migrated,
        "slack",
        connector_id,
        [
            AclRecord(
                target=SourceRef(source_type=CHANNEL, source_id="C-1"),
                principal_source_id="U-NOBODY",
            )
        ],
    )

    assert written == 0
    assert scalar(migrated, "SELECT count(*) FROM acl_source_grants") == 0


def test_an_acl_refresh_removes_revoked_grants(migrated: Connection, connector_id: UUID) -> None:
    """The revocation story: a grant that stops being emitted stops existing."""
    persist_identities(
        migrated,
        "slack",
        connector_id,
        [
            IdentityRecord(kind="user", source_id="U-1"),
            IdentityRecord(kind="user", source_id="U-2"),
        ],
    )
    target = SourceRef(source_type=CHANNEL, source_id="C-1")
    persist_acls(
        migrated,
        "slack",
        connector_id,
        [
            AclRecord(target=target, principal_source_id="U-1"),
            AclRecord(target=target, principal_source_id="U-2"),
        ],
    )
    assert scalar(migrated, "SELECT count(*) FROM acl_source_grants") == 2

    persist_acls(
        migrated, "slack", connector_id, [AclRecord(target=target, principal_source_id="U-1")]
    )

    assert scalar(migrated, "SELECT count(*) FROM acl_source_grants") == 1


# ---------------------------------------------------------------------------
# Cursors.
# ---------------------------------------------------------------------------


def test_a_cursor_round_trips(migrated: Connection, connector_id: UUID) -> None:
    save_cursor(migrated, connector_id, "content", {"phase": "messages", "channel": 2})

    assert load_cursor(migrated, connector_id, "content") == {"phase": "messages", "channel": 2}


def test_an_unknown_stream_starts_at_the_beginning(
    migrated: Connection, connector_id: UUID
) -> None:
    assert load_cursor(migrated, connector_id, "content") == {}


def test_a_finished_pass_starts_over_next_time(migrated: Connection, connector_id: UUID) -> None:
    """A listing has nowhere past its end, so the terminal cursor means start
    again rather than resume."""
    save_cursor(migrated, connector_id, "identities", {"page": "", "done": True})

    assert load_cursor(migrated, connector_id, "identities") == {}


def test_schema_drift_is_logged_and_the_sync_continues(
    migrated: Connection, connector_id: UUID, caplog: pytest.LogCaptureFixture
) -> None:
    """Drift never loses data."""
    runtime = SyncRuntime(MockConnector(), connector_id)
    save_cursor(migrated, connector_id, "content", {}, schema_version="2020-01-01")

    with caplog.at_level("WARNING", logger="hippo.sync"):
        runtime.sync_stream(migrated, "content")

    assert any("schema version changed" in record.message for record in caplog.records)
    assert scalar(migrated, "SELECT count(*) FROM raw_records") > 0


def test_an_unknown_stream_name_is_rejected(migrated: Connection, connector_id: UUID) -> None:
    with pytest.raises(ValueError, match="unknown stream"):
        SyncRuntime(MockConnector(), connector_id).sync_stream(migrated, "everything")


# ---------------------------------------------------------------------------
# Rate limiting.
# ---------------------------------------------------------------------------


class RateLimitedHalfway(MockConnector):
    """Delivers one page, then the source asks us to stop."""

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        pages = super().content(cursor)
        yield next(pages)
        raise RateLimitedError("slow down", retry_after=45.0)


def test_a_rate_limit_keeps_the_progress_already_made(
    migrated: Connection, connector_id: UUID
) -> None:
    """Page-at-a-time commits mean a backoff costs the remaining pages, never
    the ones already stored."""
    runtime = SyncRuntime(RateLimitedHalfway(), connector_id)

    outcome = runtime.sync_stream(migrated, "content")

    assert outcome.complete is False
    assert outcome.rate_limited_for == 45.0
    assert outcome.pages == 1
    assert scalar(migrated, "SELECT count(*) FROM raw_records") == outcome.records
    assert load_cursor(migrated, connector_id, "content") == {"offset": 2}
    assert "rate limited" in str(
        scalar(migrated, "SELECT last_error FROM sync_state WHERE stream = 'content'")
    )


def test_the_next_run_resumes_where_the_rate_limit_stopped(
    migrated: Connection, connector_id: UUID
) -> None:
    SyncRuntime(RateLimitedHalfway(), connector_id).sync_stream(migrated, "content")
    before = scalar(migrated, "SELECT count(*) FROM raw_records")

    outcome = SyncRuntime(MockConnector(), connector_id).sync_stream(migrated, "content")

    assert outcome.complete is True
    assert scalar(migrated, "SELECT count(*) FROM raw_records") > before


# ---------------------------------------------------------------------------
# A whole Slack workspace.
# ---------------------------------------------------------------------------


def test_syncing_slack_populates_every_table(
    migrated: Connection, connector_id: UUID, slack_runtime: SyncRuntime
) -> None:
    outcomes = slack_runtime.sync_all(migrated)

    assert all(outcome.complete for outcome in outcomes.values())
    assert scalar(migrated, "SELECT count(*) FROM principals") == 4  # 3 users + workspace group
    assert (
        scalar(migrated, "SELECT count(*) FROM raw_records WHERE source_type='slack.channel'") == 2
    )
    assert (
        scalar(migrated, "SELECT count(*) FROM raw_records WHERE source_type='slack.message'") == 8
    )
    assert scalar(migrated, "SELECT count(*) FROM acl_source_grants") == 3


def test_syncing_twice_changes_nothing(
    migrated: Connection, connector_id: UUID, slack_runtime: SyncRuntime
) -> None:
    """Full resync is a normal operation, not a duplication event."""
    slack_runtime.sync_all(migrated)
    before = scalar(migrated, "SELECT count(*) FROM raw_records")

    SyncRuntime(SlackConnector(FixtureTransport(SLACK_FIXTURES)), connector_id).sync_all(migrated)

    assert scalar(migrated, "SELECT count(*) FROM raw_records") == before
