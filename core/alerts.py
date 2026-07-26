"""Raising alarms, and telling somebody.

Two failure modes are silent by construction and this is what makes them
audible.

**Schema drift.** A connector version changes, extraction reads slightly less,
and answers get slightly worse for weeks. Nothing breaks, so nothing pages.

**Sync failure.** A stream that has been failing for a day looks, from every
other surface, exactly like a stream with nothing to do. For the ACL stream
those two states are a stale permission and a current one, which ARCHITECTURE
§11 puts under the security story rather than under performance.

**A notification must never break the thing it is watching.** Every function
here swallows its own failures. A webhook that took down a sync because someone
rotated a Slack URL would be a monitoring system causing the outage it exists
to report — so delivery failures are logged, recorded, and retried on the next
occurrence rather than raised.

**Notified once, not every tick.** The ACL stream runs every four minutes; a
failing one would deliver three hundred and sixty messages a day and be muted
within the hour. The webhook fires when an alert is first raised and again if
it recurs after somebody cleared it, because "we fixed it and it came back" is
different news from "it is still broken".
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict

from core.db import Connection

LOG = logging.getLogger("hippo.alerts")

DELIVERED = Counter(
    "hippo_alert_notifications_total", "Webhook deliveries attempted.", ("kind", "outcome")
)

SCHEMA_DRIFT = "schema_drift"
SYNC_FAILURE = "sync_failure"
ACL_STALE = "acl_stale"
ACTION_STUCK = "action_stuck"

# Long enough that a flapping stream does not spam, short enough that a real
# outage is reported while somebody still cares.
RENOTIFY_AFTER_HOURS = 6


class Alert(BaseModel):
    """Something an operator should look at."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    connector_id: UUID | None
    connector: str | None
    stream: str | None
    detail: str
    occurrences: int
    first_seen_at: datetime
    last_seen_at: datetime
    notified_at: datetime | None

    @property
    def headline(self) -> str:
        where = " ".join(part for part in (self.connector, self.stream) if part)
        return f"{self.kind.replace('_', ' ')}: {where}" if where else self.kind

    @property
    def is_recurring(self) -> bool:
        """Repeats matter. One failed sync is a blip; ninety is an outage."""
        return self.occurrences > 1


def raise_alert(
    conn: Connection,
    kind: str,
    detail: str,
    *,
    connector_id: UUID | None = None,
    stream: str | None = None,
    fingerprint: str | None = None,
) -> UUID | None:
    """Record a problem, deduplicated against the same problem already open.

    Never raises. A failure to record an alarm must not become a second, larger
    failure in the thing that noticed — the caller is usually mid-sync and its
    job is to finish or fail on its own terms.

    The fingerprint defaults to the detail, so a changed error message is a new
    alert and the same one recurring is not.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT raise_alert(%s, %s, %s, %s, %s)",
                (kind, connector_id, stream, fingerprint or detail, detail),
            )
            row = cur.fetchone()
    except Exception as exc:
        LOG.error("could not record an alert", extra={"kind": kind, "error": str(exc)})
        return None

    LOG.warning("alert raised", extra={"kind": kind, "stream": stream, "detail": detail[:200]})
    return None if row is None else UUID(str(row[0]))


def open_alerts(conn: Connection, limit: int = 100) -> list[Alert]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM open_alerts(%s)", (limit,))
        columns = [description.name for description in cur.description or []]
        return [
            Alert.model_validate(dict(zip(columns, row, strict=True))) for row in cur.fetchall()
        ]


def acknowledge(conn: Connection, alert_id: UUID, principal_id: UUID) -> bool:
    """Say a person saw it.

    Not a delete: that somebody looked is a fact worth keeping, and the same
    problem recurring afterwards is news rather than noise.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT acknowledge_alert(%s, %s)", (alert_id, principal_id))
        row = cur.fetchone()
    cleared = bool(row and row[0])
    if cleared:
        LOG.info("alert acknowledged", extra={"alert": str(alert_id)})
    return cleared


def notify(
    conn: Connection,
    webhook_url: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 5.0,
) -> int:
    """Deliver anything open that has not been reported recently.

    Returns how many went out. Never raises: a webhook that took down a sync
    because someone rotated a URL would be a monitoring system causing the
    outage it exists to report.
    """
    if not webhook_url:
        return 0

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, kind, stream, detail, occurrences FROM alerts "
            "WHERE acknowledged_at IS NULL "
            "  AND (notified_at IS NULL OR notified_at < now() - make_interval(hours => %s)) "
            "ORDER BY last_seen_at DESC LIMIT 20",
            (RENOTIFY_AFTER_HOURS,),
        )
        pending = cur.fetchall()

    if not pending:
        return 0

    http = client or httpx.Client(timeout=timeout)
    sent = 0
    for alert_id, kind, stream, detail, occurrences in pending:
        body = {
            "source": "hippo",
            "kind": str(kind),
            "stream": stream,
            "detail": str(detail),
            "occurrences": int(occurrences),
            # A generic shape rather than a Slack-specific one. `text` is what
            # Slack and most incoming-webhook receivers render, and everything
            # else is there for anything that parses.
            "text": f"Hippo {str(kind).replace('_', ' ')}: {detail}",
        }
        try:
            response = http.post(webhook_url, json=body)
            response.raise_for_status()
        except Exception as exc:
            DELIVERED.labels(kind=str(kind), outcome="failed").inc()
            LOG.error(
                "alert webhook failed",
                extra={"kind": str(kind), "error": str(exc)[:200]},
            )
            continue

        DELIVERED.labels(kind=str(kind), outcome="delivered").inc()
        # Marked only on success, so a failed delivery is retried on the next
        # sweep rather than lost.
        with conn.cursor() as cur:
            cur.execute("UPDATE alerts SET notified_at = now() WHERE id = %s", (alert_id,))
        sent += 1

    return sent


def summarise(conn: Connection) -> dict[str, Any]:
    """Counts for the metrics collector and the UI banner."""
    with conn.cursor() as cur:
        cur.execute("SELECT kind, count(*) FROM alerts WHERE acknowledged_at IS NULL GROUP BY kind")
        by_kind = {str(row[0]): int(row[1]) for row in cur.fetchall()}
    return {"total": sum(by_kind.values()), "by_kind": by_kind}
