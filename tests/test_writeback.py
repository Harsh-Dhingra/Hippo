"""P1-SYNC-5's done-condition: ARCHITECTURE §12 point 3.

    "Add a comment on JIRA-123 summarizing this" → pending action → approve in
    UI → comment appears in Jira → rollback in UI → comment gone.

The last two steps are what this fragment adds, and they are where CLAUDE.md
rule 3 stops being a sentence: no inverse capture means the action fails, and
that is enforced in the shape of the connector interface, in the executor, and
in a database CHECK. All three are tested, because a rule enforced once is a
rule one refactor away from being a convention.

Jira is a fixture. The transport records what was written and simulates enough
state that a rollback is observable — a comment gets an id the delete has to
name, and a transition changes what the next capture reads. Testing rollback
against a transport that forgot the write would be testing nothing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from core.db import Connection
from sync.connectors.jira import FixtureTransport, JiraConnector
from sync.connectors.sdk import (
    InverseCaptureError,
    PermanentSourceError,
    SourceRef,
    WritebackRequest,
    perform_writeback,
)
from sync.writeback import (
    STUCK,
    due_actions,
    execute_action,
    reap_stuck_executions,
    rollback_action,
)

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"

COMMENT = "Summary: legal review is the blocker."


@pytest.fixture
def transport() -> FixtureTransport:
    return FixtureTransport(FIXTURES)


@pytest.fixture
def connector(transport: FixtureTransport) -> JiraConnector:
    return JiraConnector(transport)


@pytest.fixture
def world(migrated: Connection) -> dict[str, Any]:
    """A Jira connector, an issue entity, and a principal who asked."""
    connector_id, entity_id, principal_id = uuid4(), uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) "
            "VALUES (%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
            (connector_id,),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id) "
            "VALUES (%s, 'user', %s, 'u-alice')",
            (principal_id, connector_id),
        )
        cur.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'ticket', 'ACME-1')",
            (entity_id,),
        )
        cur.execute(
            "INSERT INTO raw_records (connector_id, source_type, source_id, payload) "
            "VALUES (%s, 'jira.issue', 'ACME-1', '{}') RETURNING id",
            (connector_id,),
        )
        raw_id = (cur.fetchone() or (None,))[0]
        cur.execute(
            "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)",
            (entity_id, raw_id),
        )
    return {"connector_id": connector_id, "entity_id": entity_id, "principal_id": principal_id}


def approved_action(
    conn: Connection, world: dict[str, Any], action_type: str = "jira.comment", **payload: Any
) -> UUID:
    """An action that has already been through propose and approve."""
    action_id = uuid4()
    body = payload or {"body": COMMENT}
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (id, requested_by, connector_id, action_type, target_entity, "
            "    payload, risk_class, status, approved_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'consequential', 'approved', %s)",
            (
                action_id,
                world["principal_id"],
                world["connector_id"],
                action_type,
                world["entity_id"],
                json.dumps(body),
                world["principal_id"],
            ),
        )
    return action_id


def row(conn: Connection, action_id: UUID) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, inverse_payload, receipt, executed_at, rolled_back_at, "
            "       rolled_back_by, error FROM actions WHERE id = %s",
            (action_id,),
        )
        found = cur.fetchone()
    assert found is not None
    return {
        "status": found[0],
        "inverse_payload": found[1],
        "receipt": found[2],
        "executed_at": found[3],
        "rolled_back_at": found[4],
        "rolled_back_by": found[5],
        "error": found[6],
    }


def factory(connector: JiraConnector) -> Any:
    return lambda _conn, _id: connector


# ---------------------------------------------------------------------------
# §12 point 3: approve, the comment appears, roll back, it is gone.
# ---------------------------------------------------------------------------


def test_an_approved_comment_is_written_to_jira(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    action_id = approved_action(migrated, world)

    assert execute_action(migrated, action_id, factory(connector)) == "executed"

    assert transport.writes == [("issue/ACME-1/comment", {"body": COMMENT})]
    assert row(migrated, action_id)["status"] == "executed"


def test_the_receipt_is_recorded(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    """What makes an executed action checkable against Jira afterwards."""
    action_id = approved_action(migrated, world)

    execute_action(migrated, action_id, factory(connector))

    assert row(migrated, action_id)["receipt"]["external_id"] == "10001"


def test_rolling_back_deletes_the_comment(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    """The last step of the demo. The id came from the receipt, because the
    inverse of creating something is deleting it and the id does not exist
    until the create returns."""
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))

    assert rollback_action(migrated, action_id, world["principal_id"], factory(connector)) == (
        "rolled_back"
    )

    assert transport.deletes == ["issue/ACME-1/comment/10001"]
    assert row(migrated, action_id)["status"] == "rolled_back"


def test_a_rollback_names_who_asked_for_it(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))

    rollback_action(migrated, action_id, world["principal_id"], factory(connector))

    record = row(migrated, action_id)
    assert record["rolled_back_by"] == world["principal_id"]
    assert record["rolled_back_at"] is not None


# ---------------------------------------------------------------------------
# Transitions: the case where the inverse is genuinely knowable beforehand.
# ---------------------------------------------------------------------------


def test_a_transition_captures_the_status_it_is_leaving(
    connector: JiraConnector,
) -> None:
    """The whole reason capture happens before execution. ACME-1 is 'In
    Progress', so that is what a rollback has to return it to."""
    request = WritebackRequest(
        action_type="jira.transition",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"to_status": "Done"},
    )

    inverse = connector.capture_inverse(request)

    assert inverse == {"op": "transition", "issue": "ACME-1", "to_status": "In Progress"}


def test_a_transition_moves_the_issue(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    action_id = approved_action(migrated, world, "jira.transition", to_status="Done")

    assert execute_action(migrated, action_id, factory(connector)) == "executed"

    assert transport.statuses["ACME-1"] == "Done"


def test_rolling_back_a_transition_restores_the_old_status(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    action_id = approved_action(migrated, world, "jira.transition", to_status="Done")
    execute_action(migrated, action_id, factory(connector))

    rollback_action(migrated, action_id, world["principal_id"], factory(connector))

    assert transport.statuses["ACME-1"] == "In Progress"


def test_a_status_the_workflow_does_not_offer_fails_loudly(connector: JiraConnector) -> None:
    """Silently doing nothing would report success for a status change that did
    not happen."""
    request = WritebackRequest(
        action_type="jira.transition",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"to_status": "Abandoned"},
    )

    with pytest.raises(PermanentSourceError, match="no transition to"):
        connector.execute(request)


def test_the_failure_says_what_was_available(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.transition",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"to_status": "Abandoned"},
    )

    with pytest.raises(PermanentSourceError) as caught:
        connector.execute(request)

    assert "Done" in str(caught.value)


# ---------------------------------------------------------------------------
# Rule 3: no inverse capture, no execution. Enforced three times.
# ---------------------------------------------------------------------------


def test_the_interface_makes_capture_a_separate_call(connector: JiraConnector) -> None:
    """perform_writeback is what orders them, so no caller has to remember."""
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"body": COMMENT},
    )

    inverse, receipt = perform_writeback(connector, request)

    assert inverse["op"] == "delete_comment"
    assert receipt.external_id is not None


def test_an_unreadable_target_is_never_executed(
    migrated: Connection, world: dict[str, Any], transport: FixtureTransport
) -> None:
    """Rule 3's actual wording: no inverse capture means the action fails. It
    does not mean the action runs without a rollback path."""
    connector = JiraConnector(transport)
    with migrated.cursor() as cur:
        cur.execute(
            "UPDATE raw_records SET source_id = 'NOSUCH-1' WHERE connector_id = %s",
            (world["connector_id"],),
        )
    action_id = approved_action(migrated, world)

    assert execute_action(migrated, action_id, factory(connector)) == "failed"

    assert transport.writes == []
    assert "InverseCaptureError" in row(migrated, action_id)["error"]


def test_capture_refuses_an_issue_it_cannot_read(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="NOSUCH-1"),
        payload={"body": COMMENT},
    )

    with pytest.raises(InverseCaptureError):
        connector.capture_inverse(request)


def test_the_database_refuses_an_execution_with_no_inverse(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """The third enforcement. Even a bug in the executor cannot record an
    execution with no way back."""
    action_id = approved_action(migrated, world)

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            "UPDATE actions SET status = 'executed', executed_at = now() WHERE id = %s",
            (action_id,),
        )
    migrated.rollback()


def test_a_rollback_with_no_recorded_id_refuses(connector: JiraConnector) -> None:
    """Deleting a guessed comment is worse than refusing to delete one."""
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"body": COMMENT},
    )

    with pytest.raises(InverseCaptureError, match="nothing safe to delete"):
        connector.rollback(request, {"op": "delete_comment", "issue": "ACME-1"})


# ---------------------------------------------------------------------------
# Claiming, and what happens when things go wrong.
# ---------------------------------------------------------------------------


def test_only_an_approved_action_executes(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    """Rule 2 from the other side: a pending action is not something this
    process may perform, whatever else is true."""
    action_id = approved_action(migrated, world)
    migrated.execute(
        "UPDATE actions SET status = 'pending', approved_by = NULL WHERE id = %s", (action_id,)
    )

    assert execute_action(migrated, action_id, factory(connector)) == "skipped"

    assert transport.writes == []


def test_executing_twice_writes_once(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    """The claim is the status change, so a second worker matches no row. 'The
    comment appeared twice' is a bad way for this system to fail."""
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))

    assert execute_action(migrated, action_id, factory(connector)) == "skipped"

    assert len(transport.writes) == 1


def test_a_failed_write_does_not_undo_anything(
    migrated: Connection, world: dict[str, Any], transport: FixtureTransport
) -> None:
    """The write may have half-landed, and guessing which half is worse than
    leaving a person to look."""

    class Exploding(JiraConnector):
        def execute(self, request: WritebackRequest) -> Any:
            raise PermanentSourceError("jira said no")

    action_id = approved_action(migrated, world)

    assert execute_action(migrated, action_id, factory(Exploding(transport))) == "failed"

    record = row(migrated, action_id)
    assert record["status"] == "failed"
    assert "jira said no" in record["error"]
    assert transport.deletes == []


def test_a_failed_rollback_leaves_the_action_executed(
    migrated: Connection, world: dict[str, Any], transport: FixtureTransport
) -> None:
    """Not 'failed'. The action did happen and the undo did not, and a status
    saying otherwise would send someone looking for a change that is still
    live in Jira."""
    connector = JiraConnector(transport)
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))

    class Stubborn(JiraConnector):
        def rollback(self, request: WritebackRequest, inverse: Any) -> None:
            raise PermanentSourceError("jira said no")

    result = rollback_action(
        migrated, action_id, world["principal_id"], factory(Stubborn(transport))
    )

    assert result == "failed"
    record = row(migrated, action_id)
    assert record["status"] == "executed"
    assert "rollback failed" in record["error"]


def test_only_an_executed_action_rolls_back(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    action_id = approved_action(migrated, world)

    assert (
        rollback_action(migrated, action_id, world["principal_id"], factory(connector)) == "skipped"
    )


def test_rolling_back_twice_deletes_once(
    migrated: Connection,
    world: dict[str, Any],
    connector: JiraConnector,
    transport: FixtureTransport,
) -> None:
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))
    rollback_action(migrated, action_id, world["principal_id"], factory(connector))

    rollback_action(migrated, action_id, world["principal_id"], factory(connector))

    assert len(transport.deletes) == 1


# ---------------------------------------------------------------------------
# A crashed executor.
# ---------------------------------------------------------------------------


def test_an_abandoned_execution_is_reaped_to_failed(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """Deliberately not a retry: the write may have half-landed, and repeating
    it blindly is how one approval becomes two comments."""
    action_id = approved_action(migrated, world)
    migrated.execute(
        "UPDATE actions SET status = 'executing', "
        "execution_started_at = now() - interval '1 hour' WHERE id = %s",
        (action_id,),
    )

    assert reap_stuck_executions(migrated) == 1

    record = row(migrated, action_id)
    assert record["status"] == "failed"
    assert record["error"] == STUCK


def test_a_recent_execution_is_left_alone(migrated: Connection, world: dict[str, Any]) -> None:
    action_id = approved_action(migrated, world)
    migrated.execute(
        "UPDATE actions SET status = 'executing', execution_started_at = now() WHERE id = %s",
        (action_id,),
    )

    assert reap_stuck_executions(migrated) == 0


def test_an_abandoned_rollback_is_reaped_too(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    """To 'failed', not back to 'executed': the undo may also have half-landed,
    and claiming the original change is intact would be a guess in the more
    dangerous direction."""
    action_id = approved_action(migrated, world)
    execute_action(migrated, action_id, factory(connector))
    migrated.execute(
        "UPDATE actions SET status = 'rolling_back', "
        "execution_started_at = now() - interval '1 hour' WHERE id = %s",
        (action_id,),
    )

    assert reap_stuck_executions(migrated) == 1
    assert row(migrated, action_id)["status"] == "failed"


# ---------------------------------------------------------------------------
# The queue.
# ---------------------------------------------------------------------------


def test_approved_actions_are_what_is_due(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    action_id = approved_action(migrated, world)

    assert due_actions(migrated) == [action_id]

    execute_action(migrated, action_id, factory(connector))
    assert due_actions(migrated) == []


def test_a_declined_action_is_never_due(migrated: Connection, world: dict[str, Any]) -> None:
    action_id = approved_action(migrated, world)
    migrated.execute(
        "UPDATE actions SET status = 'declined', declined_by = %s WHERE id = %s",
        (world["principal_id"], action_id),
    )

    assert due_actions(migrated) == []


def test_a_connector_that_cannot_write_is_refused(migrated: Connection) -> None:
    """Being able to read is not being able to write. Slack is read-only in v0
    and has no write-back methods at all."""
    from sync.worker import build_writeback_connector

    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
        (connector_id,),
    )
    migrated.commit()

    import os

    os.environ["HIPPO_SLACK_TOKEN"] = "xoxb-test"
    try:
        with pytest.raises(PermanentSourceError, match="cannot perform write-backs"):
            build_writeback_connector(migrated, connector_id)
    finally:
        del os.environ["HIPPO_SLACK_TOKEN"]


def test_an_action_type_jira_does_not_know_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.delete",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={},
    )

    with pytest.raises(PermanentSourceError, match="cannot perform"):
        connector.capture_inverse(request)
    with pytest.raises(PermanentSourceError, match="cannot perform"):
        connector.execute(request)


def test_an_action_with_no_target_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(action_type="jira.comment", target=None, payload={"body": "x"})

    with pytest.raises(PermanentSourceError, match="needs a target"):
        connector.capture_inverse(request)


def test_a_comment_target_may_be_a_comment_reference(connector: JiraConnector) -> None:
    """Comments are addressed as ISSUE:comment_id elsewhere in the system; the
    issue key is the part before the colon either way."""
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.comment", source_id="ACME-1:10100"),
        payload={"body": COMMENT},
    )

    assert connector.capture_inverse(request)["issue"] == "ACME-1"


def test_an_empty_comment_body_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"body": ""},
    )

    with pytest.raises(PermanentSourceError, match="needs a body"):
        connector.execute(request)


def test_an_empty_target_status_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.transition",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"to_status": ""},
    )

    with pytest.raises(PermanentSourceError, match="needs a target status"):
        connector.execute(request)


def test_an_unknown_inverse_operation_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={},
    )

    with pytest.raises(PermanentSourceError, match="unknown inverse operation"):
        connector.rollback(request, {"op": "detonate", "issue": "ACME-1"})


# ---------------------------------------------------------------------------
# Degenerate shapes, where refusing beats guessing.
# ---------------------------------------------------------------------------


def test_an_action_whose_target_has_no_source_reference_fails(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    """An entity with no raw record from this connector cannot be addressed in
    Jira's terms, so there is nothing to write to."""
    action_id = approved_action(migrated, world)
    migrated.execute("DELETE FROM entity_sources")

    assert execute_action(migrated, action_id, factory(connector)) == "failed"
    assert "needs a target issue" in row(migrated, action_id)["error"]


def test_an_action_with_no_target_entity_fails(
    migrated: Connection, world: dict[str, Any], connector: JiraConnector
) -> None:
    action_id = approved_action(migrated, world)
    migrated.execute("UPDATE actions SET target_entity = NULL WHERE id = %s", (action_id,))

    assert execute_action(migrated, action_id, factory(connector)) == "failed"


def test_an_issue_with_no_status_cannot_be_transitioned(
    tmp_path: Path, migrated: Connection, world: dict[str, Any]
) -> None:
    """No status to return to means no proven rollback path, and rule 3 says
    that fails rather than proceeding."""
    import shutil

    shutil.copytree(FIXTURES, tmp_path / "jira")
    (tmp_path / "jira" / "issue.ACME-1.json").write_text(
        json.dumps({"key": "ACME-1", "fields": {"summary": "no status here"}})
    )
    connector = JiraConnector(FixtureTransport(tmp_path / "jira"))
    action_id = approved_action(migrated, world, "jira.transition", to_status="Done")

    assert execute_action(migrated, action_id, factory(connector)) == "failed"
    assert "no current status" in row(migrated, action_id)["error"]


def test_a_response_with_no_fields_is_not_a_capture(
    tmp_path: Path, connector: JiraConnector
) -> None:
    import shutil

    shutil.copytree(FIXTURES, tmp_path / "jira")
    (tmp_path / "jira" / "issue.ACME-1.json").write_text(json.dumps({"key": "ACME-1"}))
    broken = JiraConnector(FixtureTransport(tmp_path / "jira"))
    request = WritebackRequest(
        action_type="jira.comment",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"body": COMMENT},
    )

    with pytest.raises(InverseCaptureError, match="no fields"):
        broken.capture_inverse(request)


def test_rolling_back_to_an_empty_status_is_refused(connector: JiraConnector) -> None:
    request = WritebackRequest(
        action_type="jira.transition",
        target=SourceRef(source_type="jira.issue", source_id="ACME-1"),
        payload={"to_status": "Done"},
    )

    with pytest.raises(PermanentSourceError, match="no target status"):
        connector.rollback(request, {"op": "transition", "issue": "ACME-1", "to_status": ""})


# ---------------------------------------------------------------------------
# Through the queue, as the worker runs it.
# ---------------------------------------------------------------------------


def test_the_worker_executes_and_rolls_back_through_jobs(
    migrated: Connection, world: dict[str, Any], db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handlers the sync process actually registers, driven by job
    payloads rather than by direct calls."""
    from core.jobs import Job
    from sync.worker import handlers
    from sync.writeback import JOB_KIND as ACTION_KIND
    from sync.writeback import ROLLBACK_KIND

    transport = FixtureTransport(FIXTURES)
    monkeypatch.setattr(
        "sync.worker.build_writeback_connector", lambda _conn, _id: JiraConnector(transport)
    )
    action_id = approved_action(migrated, world)
    migrated.commit()
    table = handlers(db_dsn)

    table[ACTION_KIND](
        Job(
            id=uuid4(),
            kind=ACTION_KIND,
            payload={"action_id": str(action_id)},
            attempts=1,
            max_attempts=5,
        )
    )
    assert transport.writes

    table[ROLLBACK_KIND](
        Job(
            id=uuid4(),
            kind=ROLLBACK_KIND,
            payload={"action_id": str(action_id), "requested_by": str(world["principal_id"])},
            attempts=1,
            max_attempts=5,
        )
    )

    assert transport.deletes
    assert row(migrated, action_id)["status"] == "rolled_back"


def test_a_writeback_capable_connector_is_accepted(
    migrated: Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sync.worker import build_writeback_connector

    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) "
        'VALUES (%s, \'jira\', \'Jira\', \'{"base_url": "https://x", "email": "b@x"}\')',
        (connector_id,),
    )
    monkeypatch.setenv("HIPPO_JIRA_TOKEN", "api-token")

    assert isinstance(build_writeback_connector(migrated, connector_id), JiraConnector)


def test_a_tick_enqueues_approved_actions(migrated: Connection, world: dict[str, Any]) -> None:
    """A human clicking approve should not wait for a sync interval."""
    from sync.worker import schedule_tick

    action_id = approved_action(migrated, world)

    schedule_tick(migrated)

    with migrated.cursor() as cur:
        cur.execute("SELECT payload FROM jobs WHERE kind = 'action.execute'")
        rows = cur.fetchall()
    assert [r[0]["action_id"] for r in rows] == [str(action_id)]
