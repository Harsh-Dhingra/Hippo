"""The sync process.

Two responsibilities, deliberately in one process: enqueue what is due, and run
what is queued. A scheduler nobody consumes is half a feature, and splitting
them into two deployables before there is a reason to would be two things to
operate instead of one.

CREDENTIALS LIVE HERE AND NOWHERE ELSE

ARCHITECTURE §4: "the sync worker role alone holds the source-system
credentials", and CLAUDE.md rule 4 keeps them out of the repo and out of the
database. So `connectors.config` carries a workspace URL and a base URL, and
the token comes from the environment, named after the connector so one
deployment can hold several:

    HIPPO_SLACK_TOKEN_<CONNECTOR_ID>   or   HIPPO_SLACK_TOKEN
    HIPPO_JIRA_TOKEN_<CONNECTOR_ID>    or   HIPPO_JIRA_TOKEN

A connector with no token does not fall back to anything. It fails its job with
a message naming the variable it wanted, because a sync that silently does
nothing looks exactly like a sync with nothing to do — and for the ACL stream
those two states are a stale permission and a current one.

Jira also needs the account email its token belongs to. That is not a secret,
so it lives in connectors.config where an operator can see which account a
sync is acting as, with an environment fallback for single-connector setups.

Each handler opens its own connection rather than borrowing the worker's. The
worker's is autocommit and is what claim and complete are fenced on; a full ACL
refresh needs a transaction of its own, and a long content sync should not sit
on the connection the lease depends on.
"""

from __future__ import annotations

import logging
import os
from uuid import UUID

from core.config import Settings
from core.db import Connection, connect
from core.jobs import Handler, Job, Worker, WorkerConfig
from sync.connectors.jira import HttpTransport as JiraHttp
from sync.connectors.jira import JiraConnector
from sync.connectors.sdk import PermanentSourceError, ReadConnector
from sync.connectors.slack import HttpTransport as SlackHttp
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime
from sync.scheduler import JOB_KIND, Cadence, acl_staleness, enqueue_due

LOG = logging.getLogger("hippo.sync.worker")

SCHEDULE_KIND = "sync.schedule"


def token_for(kind: str, connector_id: UUID) -> str:
    """The credential for one connector, from the environment.

    Per-connector first, so two Slack workspaces in one deployment do not have
    to share a token; the unsuffixed name is the single-connector convenience.
    """
    specific = f"HIPPO_{kind.upper()}_TOKEN_{str(connector_id).replace('-', '_').upper()}"
    general = f"HIPPO_{kind.upper()}_TOKEN"
    token = os.environ.get(specific) or os.environ.get(general)
    if not token:
        raise PermanentSourceError(
            f"no credential for {kind} connector {connector_id}: set {specific} or {general}"
        )
    return token


def build_connector(conn: Connection, connector_id: UUID) -> ReadConnector:
    """The live connector for one row of the registry.

    Config comes from the database, the credential from the environment, and
    the two are never stored together.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT kind, config FROM connectors WHERE id = %s", (connector_id,))
        row = cur.fetchone()
    if row is None:
        raise PermanentSourceError(f"no connector {connector_id}")

    kind, config = str(row[0]), dict(row[1] or {})
    token = token_for(kind, connector_id)

    if kind == "slack":
        return SlackConnector(SlackHttp(token))
    if kind == "jira":
        base_url = str(config.get("base_url") or "")
        if not base_url:
            raise PermanentSourceError(f"jira connector {connector_id} has no base_url in config")
        email = str(config.get("email") or os.environ.get("HIPPO_JIRA_EMAIL") or "")
        if not email:
            raise PermanentSourceError(
                f"jira connector {connector_id} needs the account email its token belongs to: "
                "set config.email or HIPPO_JIRA_EMAIL"
            )
        return JiraConnector(JiraHttp(base_url, email, token))

    raise PermanentSourceError(f"no connector implementation for kind {kind!r}")


def sync_stream(conn: Connection, job: Job) -> None:
    """Handle one `sync.stream` job."""
    connector_id = UUID(str(job.payload["connector_id"]))
    stream = str(job.payload["stream"])
    connector = build_connector(conn, connector_id)
    outcome = SyncRuntime(connector, connector_id).sync_stream(conn, stream)
    LOG.info(
        "stream synced",
        extra={
            "connector_id": str(connector_id),
            "stream": stream,
            "records": outcome.records,
            "pages": outcome.pages,
        },
    )


def schedule_tick(conn: Connection, cadence: Cadence | None = None) -> int:
    """Enqueue whatever is due, and report how stale permissions are.

    The staleness report runs on every tick rather than after a successful
    sync, because the connector whose staleness someone needs to see is
    precisely the one whose syncs are failing.
    """
    enqueued = enqueue_due(conn, cadence)
    stale = [item for item in acl_staleness(conn) if not item.within_target]
    if stale:
        LOG.warning(
            "connectors whose permissions are past the propagation target",
            extra={"connectors": [str(item.connector_id) for item in stale]},
        )
    LOG.info("schedule tick", extra={"enqueued": len(enqueued), "stale": len(stale)})
    return len(enqueued)


def handlers(dsn: str, cadence: Cadence | None = None) -> dict[str, Handler]:
    """The handler table this process runs.

    Each opens its own connection: the worker's is autocommit and is what the
    lease fencing depends on, and a full ACL refresh needs a transaction of its
    own.
    """

    def stream(job: Job) -> None:
        with connect(dsn) as conn:
            sync_stream(conn, job)

    def schedule(_job: Job) -> None:
        with connect(dsn) as conn:
            schedule_tick(conn, cadence)

    return {JOB_KIND: stream, SCHEDULE_KIND: schedule}


def build_worker(settings: Settings, cadence: Cadence | None = None) -> Worker:
    """A worker wired to the sync handlers.

    The lease has to outlast the slowest stream, and a content sync over a
    large workspace is the slowest thing this process does.
    """
    return Worker(
        settings.database_url,
        handlers(settings.database_url, cadence),
        WorkerConfig(name=f"sync:{os.getpid()}", lease_seconds=900.0),
    )
