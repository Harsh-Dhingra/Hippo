"""P2-GOV-2: an audit log that answers questions the actions table cannot.

ARCHITECTURE §4 said "the audit log is the table itself", which was true and
was not enough. A table whose rows are overwritten answers "what is this now".
An audit answers "how did it get here", and none of that survives an UPDATE:
who declined it before someone else approved, how long it sat, what the payload
said at the moment of approval.

The tests that carry the weight are the ones about what cannot be changed. A
log the application can rewrite answers "what do we currently claim happened",
which is a different and much less useful thing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from core.audit import DEFAULT_RETENTION, events_for, purge, to_csv, to_jsonl
from core.db import Connection

pytestmark = pytest.mark.requires_db


@pytest.fixture
def world(migrated: Connection) -> dict[str, Any]:
    connector, entity = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')",
            (connector,),
        )
        cur.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'ticket', 'ACME-1')",
            (entity,),
        )
        people = {}
        for name in ("alice", "bob"):
            cur.execute(
                "INSERT INTO principals (kind, connector_id, source_id, email) "
                "VALUES ('user', %s, %s, %s) RETURNING id",
                (connector, name, f"{name}@example.com"),
            )
            people[name] = UUID(str((cur.fetchone() or (None,))[0]))
    return {"connector": connector, "entity": entity, "people": people}


def propose(conn: Connection, world: dict[str, Any], who: str = "alice") -> UUID:
    action_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (id, requested_by, connector_id, action_type, target_entity, "
            "    payload, risk_class, status, summary) "
            "VALUES (%s, %s, %s, 'jira.comment', %s, %s, 'consequential', 'pending', "
            "        'Comment on ACME-1')",
            (
                action_id,
                world["people"][who],
                world["connector"],
                world["entity"],
                json.dumps({"body": "the original text"}),
            ),
        )
    return action_id


def events(conn: Connection, action_id: UUID) -> list[tuple[str | None, str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT from_status, to_status FROM action_events WHERE action_id = %s ORDER BY id",
            (action_id,),
        )
        return [(row[0], str(row[1])) for row in cur.fetchall()]


# ---------------------------------------------------------------------------
# Every transition is recorded, by a trigger nobody can forget to call.
# ---------------------------------------------------------------------------


def test_proposing_records_the_first_event(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)

    assert events(migrated, action) == [(None, "pending")]


def test_the_whole_life_of_an_action_is_kept(migrated: Connection, world: dict[str, Any]) -> None:
    """The chain the actions table cannot hold: it would show only the last of
    these."""
    action = propose(migrated, world)
    alice = world["people"]["alice"]

    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s", (alice, action)
    )
    migrated.execute(
        "UPDATE actions SET status='executing', execution_started_at=now() WHERE id=%s", (action,)
    )
    migrated.execute(
        "UPDATE actions SET status='executed', executed_at=now(), inverse_payload='{}' WHERE id=%s",
        (action,),
    )

    assert events(migrated, action) == [
        (None, "pending"),
        ("pending", "approved"),
        ("approved", "executing"),
        ("executing", "executed"),
    ]


def test_a_decline_before_an_approval_is_not_lost(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """The question that motivated the whole fragment. In the actions table the
    decline is simply gone."""
    action = propose(migrated, world)
    alice, bob = world["people"]["alice"], world["people"]["bob"]
    migrated.execute(
        "UPDATE actions SET status='declined', declined_by=%s WHERE id=%s", (bob, action)
    )
    migrated.execute("UPDATE actions SET status='pending', declined_by=NULL WHERE id=%s", (action,))
    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s", (alice, action)
    )

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT to_status, actor FROM action_events WHERE action_id = %s ORDER BY id", (action,)
        )
        chain = [(str(row[0]), row[1]) for row in cur.fetchall()]

    assert ("declined", bob) in chain
    assert ("approved", alice) in chain


def test_an_edit_that_moves_nothing_is_not_an_event(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """Logging every UPDATE would bury the transitions under noise."""
    action = propose(migrated, world)

    migrated.execute("UPDATE actions SET error = 'a note' WHERE id = %s", (action,))

    assert events(migrated, action) == [(None, "pending")]


def test_the_snapshot_keeps_what_was_approved(migrated: Connection, world: dict[str, Any]) -> None:
    """A later edit to the payload must not rewrite what somebody agreed to."""
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )

    migrated.execute(
        "UPDATE actions SET payload = %s WHERE id = %s",
        (json.dumps({"body": "something else entirely"}), action),
    )

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT snapshot -> 'payload' ->> 'body' FROM action_events "
            "WHERE action_id = %s AND to_status = 'approved'",
            (action,),
        )
        assert cur.fetchone() == ("the original text",)


def test_a_policy_approval_is_recorded_as_a_policy(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """ "A policy" is not "a person", and an audit that rendered both as an id
    would lose the one fact P2-GOV-1 went to trouble to preserve."""
    action = propose(migrated, world)

    migrated.execute(
        "UPDATE actions SET status='approved', approved_by_policy='auto_approve:jira.comment' "
        "WHERE id=%s",
        (action,),
    )

    found = events_for(migrated, world["people"]["alice"])
    approval = next(event for event in found if event.to_status == "approved")
    assert approval.actor is None
    assert approval.decided_by == "policy: auto_approve:jira.comment"


# ---------------------------------------------------------------------------
# Append-only, and enforced rather than agreed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["hippo_api", "hippo_agent", "hippo_sync", "hippo_resolver"])
def test_no_service_role_can_rewrite_history(
    migrated: Connection, world: dict[str, Any], role: str
) -> None:
    """The value of an audit log is exactly proportional to how hard it is to
    edit."""
    propose(migrated, world)
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute(f'SET ROLE "{role}"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("UPDATE action_events SET to_status = 'executed'")
    migrated.rollback()


@pytest.mark.parametrize("role", ["hippo_api", "hippo_agent", "hippo_sync", "hippo_resolver"])
def test_no_service_role_can_delete_history(
    migrated: Connection, world: dict[str, Any], role: str
) -> None:
    """Retention is an operator action, not an application one. A log the
    application can trim is one an attacker can trim."""
    propose(migrated, world)
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute(f'SET ROLE "{role}"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("DELETE FROM action_events")
    migrated.rollback()


def test_no_service_role_can_forge_an_event(migrated: Connection, world: dict[str, Any]) -> None:
    propose(migrated, world)
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_api"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute(
                "INSERT INTO action_events (action_id, to_status) VALUES (%s, 'executed')",
                (uuid4(),),
            )
    migrated.rollback()


# ---------------------------------------------------------------------------
# Reading and filtering.
# ---------------------------------------------------------------------------


def test_the_log_is_scoped_to_the_reader(migrated: Connection, world: dict[str, Any]) -> None:
    """An audit log of other people's actions is a list of things they can
    see."""
    propose(migrated, world, who="alice")

    assert events_for(migrated, world["people"]["alice"])
    assert events_for(migrated, world["people"]["bob"]) == []


def test_filtering_by_status(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )

    approvals_only = events_for(migrated, world["people"]["alice"], status="approved")

    assert [event.to_status for event in approvals_only] == ["approved"]


def test_filtering_by_time(migrated: Connection, world: dict[str, Any]) -> None:
    propose(migrated, world)
    migrated.execute("UPDATE action_events SET at = now() - interval '2 days'")
    propose(migrated, world)

    recent = events_for(
        migrated, world["people"]["alice"], since=datetime.now(UTC) - timedelta(hours=1)
    )

    assert len(recent) == 1


def test_the_newest_event_comes_first(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )

    found = events_for(migrated, world["people"]["alice"])

    assert found[0].to_status == "approved"


def test_the_limit_is_bounded(migrated: Connection, world: dict[str, Any]) -> None:
    for _ in range(5):
        propose(migrated, world)

    assert len(events_for(migrated, world["people"]["alice"], limit=2)) == 2
    assert len(events_for(migrated, world["people"]["alice"], limit=0)) == 1


# ---------------------------------------------------------------------------
# Export. An audit that cannot leave the building is not evidence.
# ---------------------------------------------------------------------------


def test_csv_export_opens_as_a_spreadsheet(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='approved', approved_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )

    csv_text = to_csv(events_for(migrated, world["people"]["alice"]))

    lines = csv_text.strip().split("\n")
    assert lines[0].startswith("at,action_id,from_status,to_status")
    assert len(lines) == 3
    assert "decided_by" in lines[0]


def test_csv_survives_a_payload_with_a_comma(migrated: Connection, world: dict[str, Any]) -> None:
    """The summary is written by a model and will eventually contain one."""
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET summary = %s, status='approved', approved_by=%s WHERE id=%s",
        (
            'Comment on ACME-1: "legal, pricing, and scope" are all blocked',
            world["people"]["alice"],
            action,
        ),
    )

    csv_text = to_csv(events_for(migrated, world["people"]["alice"]))

    import csv as csv_module
    import io

    rows = list(csv_module.reader(io.StringIO(csv_text)))
    assert len(rows) == 3
    assert all(len(row) == len(rows[0]) for row in rows)


def test_jsonl_export_carries_the_snapshots(migrated: Connection, world: dict[str, Any]) -> None:
    """CSV leaves the payload out; anyone who needs it wants JSON anyway."""
    propose(migrated, world)

    lines = list(to_jsonl(events_for(migrated, world["people"]["alice"])))

    parsed = json.loads(lines[0])
    assert parsed["snapshot"]["payload"]["body"] == "the original text"
    assert parsed["to_status"] == "pending"


def test_jsonl_is_streamed_rather_than_assembled() -> None:
    """An export is the request most likely to be large, and holding a year of
    it in memory to hand back one string is how this becomes an outage."""
    import inspect

    assert inspect.isgeneratorfunction(to_jsonl)


def test_an_empty_export_is_still_a_valid_file(migrated: Connection, world: dict[str, Any]) -> None:
    csv_text = to_csv([])

    assert csv_text.strip().startswith("at,action_id")
    assert list(to_jsonl([])) == []


# ---------------------------------------------------------------------------
# Retention.
# ---------------------------------------------------------------------------


def test_old_events_for_finished_actions_are_purged(
    migrated: Connection, world: dict[str, Any]
) -> None:
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='declined', declined_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )
    migrated.execute("UPDATE action_events SET at = now() - interval '400 days'")

    assert purge(migrated) == 2
    assert events(migrated, action) == []


def test_recent_events_are_kept(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status='declined', declined_by=%s WHERE id=%s",
        (world["people"]["alice"], action),
    )

    assert purge(migrated) == 0


def test_a_live_action_keeps_its_history_however_old(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """An action pending for fourteen months is unusual, and is exactly the one
    whose history somebody will want."""
    action = propose(migrated, world)
    migrated.execute("UPDATE action_events SET at = now() - interval '400 days'")

    assert purge(migrated) == 0
    assert events(migrated, action) == [(None, "pending")]


def test_the_retention_window_is_a_decision_not_forever() -> None:
    """An operator who never thinks about it gets something defensible; one who
    needs a different number sets one."""
    assert timedelta(days=90) <= DEFAULT_RETENTION <= timedelta(days=3650)


def test_a_custom_window_is_honoured(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)
    migrated.execute("UPDATE actions SET status='failed', error='x' WHERE id=%s", (action,))
    migrated.execute("UPDATE action_events SET at = now() - interval '10 days'")

    assert purge(migrated, timedelta(days=30)) == 0
    assert purge(migrated, timedelta(days=5)) == 2


def test_deleting_an_action_takes_its_history(migrated: Connection, world: dict[str, Any]) -> None:
    action = propose(migrated, world)

    migrated.execute("DELETE FROM actions WHERE id = %s", (action,))

    assert events(migrated, action) == []
