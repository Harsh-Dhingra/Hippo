"""The human half of the write path.

CLAUDE.md rule 2 splits proposing from executing, and this is the join between
them: a person looks at a pending row and decides. Nothing here executes
anything either — approving sets a status, and the sync worker, which is the
only component holding source-system credentials, picks it up from there
(ARCHITECTURE §4 point 3).

Two decisions worth naming.

**Only pending actions can be decided.** Every transition is written as
`WHERE status = 'pending'`, so approving twice, approving something already
executed, or two people clicking at once cannot produce a second approval. The
row's own state is the lock; there is no read-then-write window to lose.

**The requester may approve their own action.** That is what §12 point 3
describes and it is the right default for a tool one team runs. Four-eyes
approval is a policy question that belongs next to the risk policy when someone
needs it, not a constraint to bake in now — but the audit log records requester
and approver separately either way, so the distinction survives to be enforced
later.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from core.db import Connection
from core.jobs import enqueue
from sync.writeback import ROLLBACK_KIND

LOG = logging.getLogger("hippo.api.approvals")

PENDING = "pending"
APPROVED = "approved"
DECLINED = "declined"
EXECUTED = "executed"


class ActionNotFoundError(Exception):
    """No such action, or not one this person may see.

    One exception for both, so the API cannot be used to discover that an
    action exists.
    """


class ActionConflictError(Exception):
    """The action is no longer pending."""


class Action(BaseModel):
    """A proposed action, as a person needs to see it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    action_type: str
    status: str
    risk_class: str
    payload: dict[str, Any]
    target_entity: UUID | None
    summary: str | None
    connector_id: UUID
    connector_kind: str
    requested_by: UUID
    approved_by: UUID | None
    declined_by: UUID | None
    executed_at: Any | None
    rolled_back_by: UUID | None
    error: str | None
    created_at: Any


# One human, several accounts. The agent records an action against whichever
# principal asked; a login resolves to whichever of that person's principals
# sorts first, and those are often not the same one. Scoping by principal alone
# hides a person's own actions from them — which is exactly what the demo did
# before migration 012.
_MINE = "SELECT principal_id FROM my_principals(%s)"

# No join to entities. An entity title is content — a Jira issue's title is
# its summary — and reaching it from here would be a read path around
# visible_chunks() held by the process that serves users, for the sake of one
# label. The agent stores the summary when it proposes (migration 013).
_SELECT = (
    "SELECT a.id, a.action_type, a.status, a.risk_class, a.payload, a.target_entity, "
    "       a.summary, a.connector_id, c.kind, a.requested_by, a.approved_by, a.declined_by, "
    "       a.executed_at, a.rolled_back_by, a.error, a.created_at "
    "FROM actions a "
    "JOIN connectors c ON c.id = a.connector_id "
)


def _row(row: Any) -> Action:
    return Action(
        id=UUID(str(row[0])),
        action_type=str(row[1]),
        status=str(row[2]),
        risk_class=str(row[3]),
        payload=dict(row[4] or {}),
        target_entity=None if row[5] is None else UUID(str(row[5])),
        summary=None if row[6] is None else str(row[6]),
        connector_id=UUID(str(row[7])),
        connector_kind=str(row[8]),
        requested_by=UUID(str(row[9])),
        approved_by=None if row[10] is None else UUID(str(row[10])),
        declined_by=None if row[11] is None else UUID(str(row[11])),
        executed_at=row[12],
        rolled_back_by=None if row[13] is None else UUID(str(row[13])),
        error=row[14],
        created_at=row[15],
    )


def list_actions(
    conn: Connection, principal_id: UUID, *, status: str | None = None, limit: int = 50
) -> list[Action]:
    """The actions this person asked for.

    Scoped to the requester rather than to everyone, for the same reason traces
    are: an action names a ticket, and a list of other people's actions is a
    list of things they can see.
    """
    sql = _SELECT + f"WHERE a.requested_by IN ({_MINE}) "
    params: list[Any] = [principal_id]
    if status is not None:
        sql += "AND a.status = %s "
        params.append(status)
    sql += "ORDER BY a.created_at DESC LIMIT %s"
    params.append(max(1, min(limit, 500)))

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return [_row(row) for row in cur.fetchall()]


def get_action(conn: Connection, principal_id: UUID, action_id: UUID) -> Action:
    with conn.cursor() as cur:
        cur.execute(
            _SELECT + f"WHERE a.id = %s AND a.requested_by IN ({_MINE})",
            (action_id, principal_id),
        )
        row = cur.fetchone()
    if row is None:
        raise ActionNotFoundError(str(action_id))
    return _row(row)


def approve(conn: Connection, principal_id: UUID, action_id: UUID) -> Action:
    """Record a human approval. Executes nothing.

    The sync worker takes it from here: it captures the inverse first, then
    executes, because rule 3 says an action with no captured inverse fails
    rather than running without a way back.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'approved', approved_by = %s "
            f"WHERE id = %s AND requested_by IN ({_MINE}) AND status = 'pending'",
            (principal_id, action_id, principal_id),
        )
        changed = cur.rowcount

    if changed == 0:
        _explain_failure(conn, principal_id, action_id)

    LOG.info(
        "action approved",
        extra={"action_id": str(action_id), "approved_by": str(principal_id)},
    )
    return get_action(conn, principal_id, action_id)


def decline(conn: Connection, principal_id: UUID, action_id: UUID) -> Action:
    """Say no. A distinct status from 'failed', because an action nobody wanted
    and an action that broke are different events."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE actions SET status = 'declined', declined_by = %s "
            f"WHERE id = %s AND requested_by IN ({_MINE}) AND status = 'pending'",
            (principal_id, action_id, principal_id),
        )
        changed = cur.rowcount

    if changed == 0:
        _explain_failure(conn, principal_id, action_id)

    LOG.info("action declined", extra={"action_id": str(action_id)})
    return get_action(conn, principal_id, action_id)


def request_rollback(conn: Connection, principal_id: UUID, action_id: UUID) -> Action:
    """Ask for an executed action to be undone.

    Records the request; the sync worker performs it, because it is the only
    component holding a Jira credential. The status does not move here for the
    same reason approval does not execute: this process cannot know whether the
    undo succeeded, and a status that claimed otherwise would send someone
    looking for a change that is still live.
    """
    action = get_action(conn, principal_id, action_id)
    if action.status != EXECUTED:
        raise ActionConflictError(f"action is {action.status}, not executed")

    enqueue(
        conn,
        ROLLBACK_KIND,
        payload={"action_id": str(action_id), "requested_by": str(principal_id)},
        dedupe_key=f"{ROLLBACK_KIND}:{action_id}",
        priority=10,
    )
    LOG.info(
        "rollback requested",
        extra={"action_id": str(action_id), "requested_by": str(principal_id)},
    )
    return action


def _explain_failure(conn: Connection, principal_id: UUID, action_id: UUID) -> None:
    """Nothing changed. Say which of the two reasons it was.

    get_action raises ActionNotFoundError when the action is not this person's,
    so reaching the conflict below means the row exists and is theirs and had
    already moved on.
    """
    current = get_action(conn, principal_id, action_id)
    raise ActionConflictError(f"action is {current.status}, not pending")
