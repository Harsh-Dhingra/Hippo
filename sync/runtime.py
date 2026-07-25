"""Turning connector records into rows.

The half of syncing that connectors deliberately do not do. Everything that
touches the database lives here, so a connector stays testable with fixtures
and so every permission-relevant write goes through one reviewed code path
rather than through each contributor's connector.

Three streams, three different persistence rules, each for a reason:

**identities and content commit a page at a time**, with the page's records and
its cursor in one transaction. A crash resumes at a page boundary.

**acls are always a full refresh, in one transaction.** A connector can say
"these principals can see this channel"; the record model has no way to say
"and nobody else can any more". Rebuilding the connector's whole grant set is
how a revocation becomes visible, and it is affordable because grant sets are
small. This is also exactly what P1-SYNC-4 wants: a cheap full diff every few
minutes, independent of content sync.

Unknown principals in an ACL record are skipped, not stubbed. Skipping loses a
grant, which fails closed; inventing a principal to hang a grant on fails open.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from prometheus_client import Counter
from psycopg.types.json import Jsonb

from core.db import Connection
from sync.connectors.sdk import (
    AclRecord,
    ContentRecord,
    Cursor,
    IdentityRecord,
    RateLimitedError,
    ReadConnector,
    is_terminal,
)

LOG = logging.getLogger("hippo.sync")

READ_STREAMS = ("identities", "content", "acls")

RECORDS = Counter("hippo_sync_records_total", "Records persisted.", ("connector", "stream"))
PAGES = Counter("hippo_sync_pages_total", "Pages persisted.", ("connector", "stream"))
SKIPPED_GRANTS = Counter(
    "hippo_sync_skipped_grants_total",
    "ACL grants dropped because their principal is unknown.",
    ("connector",),
)


@dataclass(frozen=True)
class StreamOutcome:
    """What one stream did."""

    stream: str
    pages: int
    records: int
    cursor: dict[str, Any]
    rate_limited_for: float | None = None

    @property
    def complete(self) -> bool:
        return self.rate_limited_for is None


# ---------------------------------------------------------------------------
# sync_state.
# ---------------------------------------------------------------------------


def load_cursor(conn: Connection, connector_id: UUID, stream: str) -> dict[str, Any]:
    """Where to resume, or the beginning.

    A terminal cursor means the last pass finished. There is nowhere past the
    end of a listing, so the next pass starts over; upserts make that idempotent
    rather than duplicative.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cursor FROM sync_state WHERE connector_id = %s AND stream = %s",
            (connector_id, stream),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return {}
    stored = dict(row[0])
    return {} if is_terminal(stored) else stored


def save_cursor(
    conn: Connection,
    connector_id: UUID,
    stream: str,
    cursor: Cursor,
    *,
    schema_version: str | None = None,
    error: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sync_state (connector_id, stream, cursor, schema_version, "
            "                        last_synced_at, last_error) "
            "VALUES (%s, %s, %s, %s, now(), %s) "
            "ON CONFLICT (connector_id, stream) DO UPDATE SET "
            "    cursor = EXCLUDED.cursor, "
            "    schema_version = EXCLUDED.schema_version, "
            "    last_synced_at = EXCLUDED.last_synced_at, "
            "    last_error = EXCLUDED.last_error",
            (connector_id, stream, Jsonb(dict(cursor)), schema_version, error),
        )


def _warn_on_drift(conn: Connection, connector_id: UUID, stream: str, declared: str) -> None:
    """Schema drift is a warning and a stored payload, never a dropped record."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_version FROM sync_state WHERE connector_id = %s AND stream = %s",
            (connector_id, stream),
        )
        row = cur.fetchone()
    previous = None if row is None else row[0]
    if previous is not None and previous != declared:
        LOG.warning(
            "connector schema version changed",
            extra={
                "connector_id": str(connector_id),
                "stream": stream,
                "previous": previous,
                "current": declared,
            },
        )


# ---------------------------------------------------------------------------
# Writes.
# ---------------------------------------------------------------------------


def _upsert_raw_record(
    conn: Connection,
    connector_id: UUID,
    source_type: str,
    source_id: str,
    payload: dict[str, Any],
    container: tuple[str, str] | None = None,
) -> UUID:
    """Upsert on (connector, source_type, source_id), which is what makes a
    full resync idempotent rather than duplicative."""
    container_type, container_id = container if container is not None else (None, None)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_records (connector_id, source_type, source_id, payload, "
            "                         container_source_type, container_source_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (connector_id, source_type, source_id) DO UPDATE SET "
            "    payload = EXCLUDED.payload, "
            "    container_source_type = EXCLUDED.container_source_type, "
            "    container_source_id = EXCLUDED.container_source_id, "
            "    fetched_at = now() "
            "RETURNING id",
            (connector_id, source_type, source_id, Jsonb(payload), container_type, container_id),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _upsert_principal(
    conn: Connection, connector_id: UUID, source_id: str, kind: str, email: str | None
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principals (kind, connector_id, source_id, email) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (connector_id, source_id) "
            "  WHERE connector_id IS NOT NULL AND source_id IS NOT NULL "
            "DO UPDATE SET kind = EXCLUDED.kind, "
            "              email = coalesce(EXCLUDED.email, principals.email) "
            "RETURNING id",
            (kind, connector_id, source_id, email),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _find_principal(conn: Connection, connector_id: UUID, source_id: str) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
            (connector_id, source_id),
        )
        row = cur.fetchone()
    return None if row is None else UUID(str(row[0]))


def _replace_memberships(
    conn: Connection, connector_id: UUID, member_id: UUID, groups: Sequence[str]
) -> None:
    """Membership is replaced per principal, not merely added to.

    Leaving a group is a permission change, and it arrives as the absence of a
    group from member_of. Only a replace can see an absence.
    """
    group_ids: list[UUID] = []
    for source_id in groups:
        # A group referenced before its own record arrives gets a stub, which
        # the real record fills in later. Membership is a fact about the user,
        # so it must not wait for the group's page.
        group_ids.append(_upsert_principal(conn, connector_id, source_id, "group", None))

    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM principal_memberships WHERE member_id = %s AND group_id <> ALL(%s)",
            (member_id, group_ids),
        )
        for group_id in group_ids:
            cur.execute(
                "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s) "
                "ON CONFLICT DO NOTHING",
                (group_id, member_id),
            )


def persist_identities(
    conn: Connection, connector_kind: str, connector_id: UUID, records: Sequence[IdentityRecord]
) -> None:
    """Identities become principals, and also raw records.

    Principals carry the permission story. The raw record carries everything
    else the source said, so the resolver can build a person entity with a name
    without sync having to interpret anything.
    """
    for record in records:
        _upsert_raw_record(
            conn,
            connector_id,
            f"{connector_kind}.{record.kind}",
            record.source_id,
            record.payload,
        )
        principal_id = _upsert_principal(
            conn, connector_id, record.source_id, record.kind, record.email
        )
        _replace_memberships(conn, connector_id, principal_id, record.member_of)


def persist_content(conn: Connection, connector_id: UUID, records: Sequence[ContentRecord]) -> None:
    for record in records:
        container = (
            (record.container.source_type, record.container.source_id)
            if record.container is not None
            else None
        )
        _upsert_raw_record(
            conn, connector_id, record.source_type, record.source_id, record.payload, container
        )


def persist_acls(
    conn: Connection, connector_kind: str, connector_id: UUID, records: Sequence[AclRecord]
) -> int:
    """Replace this connector's whole grant set. Returns grants written."""
    written = 0
    with conn.cursor() as cur:
        cur.execute("DELETE FROM acl_source_grants WHERE connector_id = %s", (connector_id,))

    for record in records:
        principal_id = _find_principal(conn, connector_id, record.principal_source_id)
        if principal_id is None:
            SKIPPED_GRANTS.labels(connector=connector_kind).inc()
            LOG.warning(
                "dropped an ACL grant naming an unknown principal",
                extra={
                    "connector_id": str(connector_id),
                    "principal_source_id": record.principal_source_id,
                    "target": f"{record.target.source_type}:{record.target.source_id}",
                },
            )
            continue
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO acl_source_grants (connector_id, target_source_type, "
                "    target_source_id, principal_id, access) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    connector_id,
                    record.target.source_type,
                    record.target.source_id,
                    principal_id,
                    record.access,
                ),
            )
        written += 1
    return written


def project_acl_grants(conn: Connection, connector_id: UUID) -> int:
    """Materialise acl_grants from source grants. Safe to run at any time."""
    with conn.cursor() as cur:
        cur.execute("SELECT project_acl_grants(%s)", (connector_id,))
        row = cur.fetchone()
    return 0 if row is None else int(row[0])


# ---------------------------------------------------------------------------
# The runtime.
# ---------------------------------------------------------------------------


class SyncRuntime:
    """Runs a connector's streams against the database."""

    def __init__(self, connector: ReadConnector, connector_id: UUID) -> None:
        self._connector = connector
        self._connector_id = connector_id

    @property
    def connector_id(self) -> UUID:
        return self._connector_id

    def sync_stream(self, conn: Connection, stream: str) -> StreamOutcome:
        if stream not in READ_STREAMS:
            msg = f"unknown stream {stream!r}; expected one of {READ_STREAMS}"
            raise ValueError(msg)
        _warn_on_drift(conn, self._connector_id, stream, self._connector.schema_version)
        if stream == "acls":
            return self._sync_acls(conn)
        return self._sync_paged(conn, stream)

    def sync_all(self, conn: Connection) -> dict[str, StreamOutcome]:
        """Identities first: content and ACLs both reference principals."""
        return {stream: self.sync_stream(conn, stream) for stream in READ_STREAMS}

    def _sync_paged(self, conn: Connection, stream: str) -> StreamOutcome:
        cursor = load_cursor(conn, self._connector_id, stream)
        pages = 0
        records = 0
        rate_limited: float | None = None
        source = getattr(self._connector, stream)

        try:
            for page in source(cursor):
                with conn.transaction():
                    if stream == "identities":
                        persist_identities(
                            conn, self._connector.kind, self._connector_id, page.records
                        )
                    else:
                        persist_content(conn, self._connector_id, page.records)
                    cursor = dict(page.cursor)
                    save_cursor(
                        conn,
                        self._connector_id,
                        stream,
                        cursor,
                        schema_version=self._connector.schema_version,
                    )
                pages += 1
                records += len(page.records)
                if not page.has_more:
                    break
        except RateLimitedError as exc:
            # Progress is already committed page by page, so backing off here
            # costs the remaining pages and never the ones already stored.
            rate_limited = exc.retry_after
            save_cursor(
                conn,
                self._connector_id,
                stream,
                cursor,
                schema_version=self._connector.schema_version,
                error=f"rate limited: {exc}",
            )
            LOG.warning(
                "sync paused by rate limit",
                extra={
                    "connector_id": str(self._connector_id),
                    "stream": stream,
                    "retry_after": exc.retry_after,
                    "pages_done": pages,
                },
            )

        PAGES.labels(connector=self._connector.kind, stream=stream).inc(pages)
        RECORDS.labels(connector=self._connector.kind, stream=stream).inc(records)
        LOG.info(
            "stream synced",
            extra={
                "connector_id": str(self._connector_id),
                "stream": stream,
                "pages": pages,
                "records": records,
            },
        )
        return StreamOutcome(
            stream=stream,
            pages=pages,
            records=records,
            cursor=cursor,
            rate_limited_for=rate_limited,
        )

    def _sync_acls(self, conn: Connection) -> StreamOutcome:
        """Always a full refresh, always from the beginning, always atomic.

        Half-applied permissions are a security bug with a delay timer, so the
        whole set lands or none of it does.
        """
        collected: list[AclRecord] = []
        pages = 0
        cursor: dict[str, Any] = {}

        for page in self._connector.acls({}):
            collected.extend(page.records)
            cursor = dict(page.cursor)
            pages += 1
            if not page.has_more:
                break

        with conn.transaction():
            written = persist_acls(conn, self._connector.kind, self._connector_id, collected)
            # Projected in the same transaction, because acl_source_grants is
            # not what the permission filter reads. Refreshing the source grants
            # and leaving the projection for a later pass would mean a
            # revocation that has landed but has not taken effect, which is the
            # one state this stream exists to make impossible.
            projected = project_acl_grants(conn, self._connector_id)
            save_cursor(
                conn,
                self._connector_id,
                "acls",
                cursor,
                schema_version=self._connector.schema_version,
            )

        PAGES.labels(connector=self._connector.kind, stream="acls").inc(pages)
        RECORDS.labels(connector=self._connector.kind, stream="acls").inc(written)
        LOG.info(
            "acls refreshed",
            extra={
                "connector_id": str(self._connector_id),
                "grants": written,
                "projected": projected,
                "dropped": len(collected) - written,
            },
        )
        return StreamOutcome(stream="acls", pages=pages, records=written, cursor=cursor)
