"""Executing approved actions, and undoing them.

The other end of CLAUDE.md rule 2. The agent inserts `pending`, a person moves
it to `approved` through the API, and this — running in the one process that
holds source-system credentials — is what actually writes to Jira.

**Rule 3 is enforced twice.** perform_writeback() captures the inverse before
executing, so a connector cannot reach execute() without one. And the database
refuses an `executed` row whose inverse_payload is NULL, so even a bug here
cannot record an execution with no way back. Two enforcements of one rule is
not redundancy; it is the difference between a convention and a guarantee.

**Claiming is a status transition, not a flag.** `UPDATE ... WHERE status =
'approved'` returning the row is what makes two workers safe: the second one
updates nothing and moves on. Reading a row and then writing it would leave a
window in which an approved action gets performed twice, and "the comment
appeared twice" is a bad outcome for a system whose selling point is that it
does not act without permission.

**A failed execution is a failure, not a rollback.** If the write raises, the
action goes to `failed` with the error, and nothing is undone: the write may
have half-landed, and guessing which half is worse than leaving a person to
look. Rollback is something a human asks for, from a state where the execution
is known to have succeeded.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from uuid import UUID

from prometheus_client import Counter
from psycopg.types.json import Jsonb

from core.db import Connection
from sync.connectors.sdk import (
    SourceRef,
    WritebackConnector,
    WritebackRequest,
    perform_writeback,
)

LOG = logging.getLogger("hippo.sync.writeback")

EXECUTED = Counter(
    "hippo_actions_executed_total",
    "Approved actions performed against a source system.",
    ("connector", "action_type", "outcome"),
)

JOB_KIND = "action.execute"
ROLLBACK_KIND = "action.rollback"

# What the connector is handed as the target. The agent stored an entity id;
# the connector needs the source reference, which entity_sources holds.
_TARGET = (
    "SELECT r.source_type, r.source_id "
    "FROM entity_sources es JOIN raw_records r ON r.id = es.raw_record_id "
    "WHERE es.entity_id = %s AND r.connector_id = %s "
    "ORDER BY r.source_type, r.source_id LIMIT 1"
)

ConnectorFactory = Callable[[Connection, UUID], WritebackConnector]


class WritebackError(RuntimeError):
    """The action could not be performed. Carries what to record."""


def claim_approved(conn: Connection, action_id: UUID) -> dict[str, Any] | None:
    """Take one approved action, or return None.

    The claim is the status change. A second worker's UPDATE matches no row,
    so it gets None rather than a duplicate write.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'executing', execution_started_at = now() "
            "WHERE id = %s AND status = 'approved' "
            "RETURNING id, connector_id, action_type, target_entity, payload",
            (action_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {
        "id": UUID(str(row[0])),
        "connector_id": UUID(str(row[1])),
        "action_type": str(row[2]),
        "target_entity": None if row[3] is None else UUID(str(row[3])),
        "payload": dict(row[4] or {}),
    }


def _target_ref(conn: Connection, entity_id: UUID | None, connector_id: UUID) -> SourceRef | None:
    if entity_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute(_TARGET, (entity_id, connector_id))
        row = cur.fetchone()
    if row is None:
        return None
    return SourceRef(source_type=str(row[0]), source_id=str(row[1]))


def execute_action(conn: Connection, action_id: UUID, factory: ConnectorFactory) -> str:
    """Perform one approved action. Returns the status it ended in.

    Everything about this function is arranged so that the only two outcomes
    are 'executed with a recorded inverse' and 'failed with a recorded reason'.
    """
    claimed = claim_approved(conn, action_id)
    if claimed is None:
        LOG.info("action was not approved and pending execution", extra={"action": str(action_id)})
        return "skipped"

    connector_id = claimed["connector_id"]
    target = _target_ref(conn, claimed["target_entity"], connector_id)
    request = WritebackRequest(
        action_type=claimed["action_type"], target=target, payload=claimed["payload"]
    )

    try:
        connector = factory(conn, connector_id)
        inverse, receipt = perform_writeback(connector, request)
    except Exception as exc:
        _record_failure(conn, action_id, f"{type(exc).__name__}: {exc}")
        EXECUTED.labels(
            connector=str(connector_id), action_type=request.action_type, outcome="failed"
        ).inc()
        LOG.warning("action failed", extra={"action": str(action_id), "error": str(exc)})
        return "failed"

    # The receipt is folded into the inverse here rather than in the connector,
    # because only a create needs it and only the caller knows the create
    # succeeded. Without this, rollback of a comment has no id to delete.
    full_inverse = dict(inverse)
    if receipt.external_id is not None:
        full_inverse["created_id"] = receipt.external_id

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'executed', executed_at = now(), "
            "    inverse_payload = %s, receipt = %s, error = NULL "
            "WHERE id = %s",
            (Jsonb(full_inverse), Jsonb(receipt.model_dump()), action_id),
        )

    EXECUTED.labels(
        connector=str(connector_id), action_type=request.action_type, outcome="executed"
    ).inc()
    LOG.info(
        "action executed",
        extra={
            "action": str(action_id),
            "action_type": request.action_type,
            "external_id": receipt.external_id,
        },
    )
    return "executed"


def rollback_action(
    conn: Connection, action_id: UUID, rolled_back_by: UUID, factory: ConnectorFactory
) -> str:
    """Undo an executed action, using the inverse captured before it ran."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'rolling_back', execution_started_at = now() "
            "WHERE id = %s AND status = 'executed' "
            "RETURNING connector_id, action_type, target_entity, payload, inverse_payload",
            (action_id,),
        )
        row = cur.fetchone()
    if row is None:
        LOG.info("action was not in a state to roll back", extra={"action": str(action_id)})
        return "skipped"

    connector_id = UUID(str(row[0]))
    inverse = dict(row[4] or {})
    request = WritebackRequest(
        action_type=str(row[1]),
        target=_target_ref(conn, None if row[2] is None else UUID(str(row[2])), connector_id),
        payload=dict(row[3] or {}),
    )

    try:
        factory(conn, connector_id).rollback(request, inverse)
    except Exception as exc:
        # Back to 'executed', not to 'failed'. The action did happen, the undo
        # did not, and a status saying otherwise would send someone looking for
        # a change that is still live in Jira.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE actions SET status = 'executed', error = %s WHERE id = %s",
                (f"rollback failed: {type(exc).__name__}: {exc}", action_id),
            )
        EXECUTED.labels(
            connector=str(connector_id),
            action_type=request.action_type,
            outcome="rollback_failed",
        ).inc()
        LOG.warning("rollback failed", extra={"action": str(action_id), "error": str(exc)})
        return "failed"

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'rolled_back', rolled_back_at = now(), "
            "    rolled_back_by = %s, error = NULL WHERE id = %s",
            (rolled_back_by, action_id),
        )

    EXECUTED.labels(
        connector=str(connector_id), action_type=request.action_type, outcome="rolled_back"
    ).inc()
    LOG.info("action rolled back", extra={"action": str(action_id)})
    return "rolled_back"


def _record_failure(conn: Connection, action_id: UUID, error: str) -> None:
    """Failure is terminal for this attempt and does not undo anything.

    The write may have half-landed, and guessing which half is worse than
    leaving a person to look at it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'failed', error = %s WHERE id = %s", (error, action_id)
        )


def due_actions(conn: Connection, limit: int = 50) -> list[UUID]:
    """Approved actions waiting to be performed."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM actions WHERE status = 'approved' ORDER BY created_at LIMIT %s",
            (max(1, min(limit, 500)),),
        )
        return [UUID(str(row[0])) for row in cur.fetchall()]


STUCK = (
    "the executor stopped mid-write, so it is not known whether this landed. "
    "Check the source system before doing anything else."
)


def reap_stuck_executions(conn: Connection, older_than_seconds: float = 900.0) -> int:
    """Move abandoned in-flight actions to 'failed'.

    Deliberately not a retry. The write may have half-landed, and repeating it
    blindly is how one approval becomes two comments. Failing with a message
    that sends a person to look is the honest outcome when the system genuinely
    does not know whether the write happened.

    A rolling_back row goes to 'failed' too rather than back to 'executed': the
    undo may also have half-landed, and claiming the original change is intact
    would be a guess in the more dangerous direction.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'failed', error = %s "
            "WHERE status IN ('executing', 'rolling_back') "
            "  AND execution_started_at < now() - make_interval(secs => %s)",
            (STUCK, older_than_seconds),
        )
        reaped = cur.rowcount
    if reaped:
        LOG.error("actions abandoned mid-write", extra={"actions": reaped})
    return reaped
