"""P1-SYNC-4's done-condition: revoke in Slack, chunk invisible within 5 min,
measured.

The measurement has two halves and both are needed.

*Does a revocation propagate at all?* An end-to-end test: Alice is in the
private channel, sees it, is removed in the source, the ACL stream runs, and
the chunk is gone. This is the half that catches a broken code path, and it
caught one — the ACL stream refreshed acl_source_grants without projecting into
acl_grants, so a revocation landed in the database and had no effect on what
anyone could read.

*How long can it take?* Not measured by sleeping for five minutes. Worst-case
staleness is `interval + one run`, so the test measures a real run against the
fixture corpus and checks the arithmetic against the configured cadence. That
is why the default interval is four minutes rather than five: a five-minute
cadence cannot keep a five-minute promise.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from core.jobs import claim
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime
from sync.scheduler import (
    ACL_PROPAGATION_TARGET,
    ACL_STREAM,
    JOB_KIND,
    Cadence,
    acl_staleness,
    due_streams,
    enqueue_due,
)
from tests.pipeline import resolve_and_enrich, visible_text

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PRIVATE_TEXT = "Acme is asking for 30 percent off to renew, do not repeat outside this channel"
PUBLIC_TEXT = "legal review is the blocker, not engineering"


class RevocableSlack(SlackFixtures):
    """The Slack fixtures, with a channel roster a test can change.

    Revocation in Slack is someone leaving a private channel, so that is what
    this models: the members call answers from a mutable set rather than from
    the file on disk.
    """

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.members: dict[str, list[str]] = {}

    def call(self, method: str, params: Any) -> dict[str, Any]:
        if method == "conversations.members":
            channel = str(params.get("channel"))
            if channel in self.members:
                self.calls.append((method, dict(params)))
                return {
                    "ok": True,
                    "members": list(self.members[channel]),
                    "response_metadata": {"next_cursor": ""},
                }
        return super().call(method, params)


@pytest.fixture
def transport() -> RevocableSlack:
    return RevocableSlack(FIXTURES / "slack")


@pytest.fixture
def workspace(migrated: Connection, transport: RevocableSlack) -> UUID:
    """A synced Slack workspace where Alice can see the private channel."""
    connector_id = uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
            (connector_id,),
        )
    SyncRuntime(SlackConnector(transport), connector_id).sync_all(migrated)
    resolve_and_enrich(migrated)
    # One more ACL pass, because the projection reads entities and the resolver
    # is what creates them. In production this is simply the next tick of the
    # fast lane: hippo_resolver has no grant on acl_grants and must not, so the
    # ACL stream is the only writer, and new content becomes visible when it
    # next runs. Erring towards invisible is the safe direction.
    SyncRuntime(SlackConnector(transport), connector_id).sync_stream(migrated, ACL_STREAM)
    return connector_id


def principal(conn: Connection, connector_id: UUID, source_id: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
            (connector_id, source_id),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def refresh_acls(conn: Connection, transport: RevocableSlack, connector_id: UUID) -> float:
    """Run the ACL stream alone, and return how long it took in seconds."""
    runtime = SyncRuntime(SlackConnector(transport), connector_id)
    started = time.monotonic()
    runtime.sync_stream(conn, ACL_STREAM)
    return time.monotonic() - started


# ---------------------------------------------------------------------------
# Does a revocation propagate at all?
# ---------------------------------------------------------------------------


def test_the_acl_stream_alone_removes_access(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """The done-condition. Nothing else runs: no content sync, no resolver
    pass, no re-projection by hand. The fast lane has to be sufficient on its
    own or it is not a fast lane."""
    alice = principal(migrated, workspace, "U-ALICE")
    assert PRIVATE_TEXT in visible_text(migrated, alice)

    transport.members["C-DEALS"] = ["U-BOB"]
    refresh_acls(migrated, transport, workspace)

    assert PRIVATE_TEXT not in visible_text(migrated, alice)


def test_the_revocation_does_not_touch_anyone_else(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """A full refresh that removed one person and everyone else with them would
    also pass the test above."""
    bob = principal(migrated, workspace, "U-BOB")

    transport.members["C-DEALS"] = ["U-BOB"]
    refresh_acls(migrated, transport, workspace)

    assert PRIVATE_TEXT in visible_text(migrated, bob)


def test_public_content_is_unaffected(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    alice = principal(migrated, workspace, "U-ALICE")

    transport.members["C-DEALS"] = ["U-BOB"]
    refresh_acls(migrated, transport, workspace)

    assert PUBLIC_TEXT in visible_text(migrated, alice)


def test_access_can_be_granted_back(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """The fast lane is a refresh, not a tombstone log, so re-adding someone
    has to work as well as removing them."""
    alice = principal(migrated, workspace, "U-ALICE")
    transport.members["C-DEALS"] = ["U-BOB"]
    refresh_acls(migrated, transport, workspace)
    assert PRIVATE_TEXT not in visible_text(migrated, alice)

    transport.members["C-DEALS"] = ["U-ALICE", "U-BOB"]
    refresh_acls(migrated, transport, workspace)

    assert PRIVATE_TEXT in visible_text(migrated, alice)


def test_the_projection_is_refreshed_by_the_stream_itself(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """acl_source_grants is not what the filter reads. A stream that refreshed
    the source grants and left the projection for a later pass would produce a
    revocation that has landed and has not taken effect."""
    alice = principal(migrated, workspace, "U-ALICE")
    transport.members["C-DEALS"] = ["U-BOB"]

    refresh_acls(migrated, transport, workspace)

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM acl_grants g "
            "JOIN chunks c ON c.entity_id = g.entity_id "
            "WHERE g.principal_id = %s AND c.content = %s",
            (alice, PRIVATE_TEXT),
        )
        assert cur.fetchone() == (0,)


def test_a_failed_refresh_changes_nothing(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """Half-applied permissions are a security bug with a delay timer. The
    transaction is what keeps the old state until a whole new one is ready."""
    alice = principal(migrated, workspace, "U-ALICE")

    class Failing(SlackConnector):
        def acls(self, cursor: Any) -> Any:
            for page in super().acls(cursor):
                # has_more, so the runtime keeps asking and reaches the failure
                # rather than stopping one page early.
                yield page.model_copy(update={"has_more": True})
            raise RuntimeError("the source went away mid-refresh")

    with pytest.raises(RuntimeError):
        SyncRuntime(Failing(transport), workspace).sync_stream(migrated, ACL_STREAM)

    assert PRIVATE_TEXT in visible_text(migrated, alice)


# ---------------------------------------------------------------------------
# How long can it take?
# ---------------------------------------------------------------------------


def test_the_measured_worst_case_is_inside_the_promise(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """The measurement PROJECT.md asks for, done by arithmetic on a real run
    rather than by waiting five minutes.

    Worst case is a full interval of waiting plus one run: a change made one
    instant after a sync starts is not picked up until the next one finishes.
    """
    transport.members["C-DEALS"] = ["U-BOB"]

    duration = timedelta(seconds=refresh_acls(migrated, transport, workspace))

    cadence = Cadence()
    worst_case = cadence.worst_case_staleness(ACL_STREAM, duration)
    assert cadence.keeps_the_acl_promise(duration), (
        f"worst case {worst_case} exceeds the {ACL_PROPAGATION_TARGET} target; "
        f"one refresh took {duration}"
    )
    assert worst_case <= ACL_PROPAGATION_TARGET


def test_the_default_cadence_leaves_room_for_the_run(migrated: Connection) -> None:
    """A five-minute cadence cannot keep a five-minute promise. The gap is the
    budget for the sync itself, and it is deliberate rather than left over."""
    cadence = Cadence()

    assert cadence.interval(ACL_STREAM) < ACL_PROPAGATION_TARGET
    headroom = ACL_PROPAGATION_TARGET - cadence.interval(ACL_STREAM)
    assert headroom >= timedelta(seconds=30)


def test_a_cadence_that_cannot_keep_the_promise_says_so(migrated: Connection) -> None:
    """The check is a function rather than a comment, so an operator who
    lengthens the interval finds out rather than assuming."""
    slow = Cadence(acls=290)

    assert slow.keeps_the_acl_promise(timedelta(seconds=1)) is True
    assert slow.keeps_the_acl_promise(timedelta(seconds=30)) is False


def test_acls_run_far_more_often_than_content(migrated: Connection) -> None:
    """ARCHITECTURE §8's whole point: the lanes have different speeds."""
    cadence = Cadence()

    assert cadence.interval(ACL_STREAM) < cadence.interval("content")
    assert cadence.interval("content") <= timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Scheduling.
# ---------------------------------------------------------------------------


def test_a_never_synced_stream_is_due_immediately(migrated: Connection) -> None:
    """A connector added between two ticks should not wait a full interval
    before anyone can see anything."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )

    due = due_streams(migrated)

    assert {item.stream for item in due} == {"identities", "content", ACL_STREAM}


def test_a_freshly_synced_stream_is_not_due(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    assert [item.stream for item in due_streams(migrated)] == []


def test_acls_come_due_before_content(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """The fast lane, observed rather than asserted about: five minutes after a
    sync, ACLs are due and content is not."""
    migrated.execute("UPDATE sync_state SET last_synced_at = now() - interval '5 minutes'")

    due = {item.stream for item in due_streams(migrated)}

    assert due == {ACL_STREAM}


def test_everything_comes_due_eventually(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    migrated.execute("UPDATE sync_state SET last_synced_at = now() - interval '20 minutes'")

    due = {item.stream for item in due_streams(migrated)}

    assert due == {"identities", "content", ACL_STREAM}


def test_due_streams_are_enqueued_as_jobs(migrated: Connection) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )

    job_ids = enqueue_due(migrated)

    assert len(job_ids) == 3
    with migrated.cursor() as cur:
        cur.execute("SELECT DISTINCT kind FROM jobs")
        assert cur.fetchall() == [(JOB_KIND,)]


def test_the_acl_job_outranks_the_content_job(migrated: Connection) -> None:
    """Under load is exactly when a revocation needs to go out before a batch
    of new messages."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    enqueue_due(migrated)

    job = claim(migrated, worker="test", kinds=(JOB_KIND,), lease_seconds=60)

    assert job is not None
    assert job.payload["stream"] == ACL_STREAM


def test_a_slow_sync_does_not_pile_up(migrated: Connection) -> None:
    """Deduplication is what makes a short cadence safe: a run that overruns
    its interval must not accumulate a queue of identical work behind it."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    enqueue_due(migrated)

    assert enqueue_due(migrated) == []

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs")
        assert cur.fetchone() == (3,)


def test_a_job_carries_what_the_worker_needs(migrated: Connection) -> None:
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    enqueue_due(migrated)

    with migrated.cursor() as cur:
        cur.execute("SELECT payload FROM jobs WHERE payload->>'stream' = %s", (ACL_STREAM,))
        payload = json.loads(json.dumps((cur.fetchone() or ({},))[0]))

    assert payload == {"connector_id": str(connector_id), "stream": ACL_STREAM}


# ---------------------------------------------------------------------------
# Is the promise being kept right now?
# ---------------------------------------------------------------------------


def test_a_freshly_synced_connector_is_within_target(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    report = acl_staleness(migrated)

    assert len(report) == 1
    assert report[0].within_target is True
    assert report[0].seconds is not None
    assert report[0].seconds < 60


def test_a_connector_whose_acls_have_stalled_is_reported(
    migrated: Connection, transport: RevocableSlack, workspace: UUID
) -> None:
    """An expired token freezes permissions at their last known state, and
    nothing else in the system would notice the security story had quietly
    stopped being true."""
    migrated.execute(
        "UPDATE sync_state SET last_synced_at = now() - interval '1 hour' WHERE stream = %s",
        (ACL_STREAM,),
    )

    report = acl_staleness(migrated)

    assert report[0].within_target is False
    assert report[0].seconds is not None
    assert report[0].seconds > ACL_PROPAGATION_TARGET.total_seconds()


def test_a_never_synced_connector_counts_as_stale(migrated: Connection) -> None:
    """Unknown is not the same as up to date, and the safe reading of unknown
    is stale."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )

    report = acl_staleness(migrated)

    assert report[0].seconds is None
    assert report[0].within_target is False
