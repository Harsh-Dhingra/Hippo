"""The four-stream connector contract.

A connector answers four questions about a source system:

    identities  who exists         -> principals
    content     what was said      -> raw_records
    acls        who can see what   -> acl grants
    writeback   how to act on it   -> executed actions

Three rules shape everything below.

**A connector never touches the database.** It yields records; the sync runtime
persists them. That is what makes a connector testable against fixture files
with no Postgres and no network, and it keeps every permission-relevant write in
one place rather than in each contributor's connector.

**A connector never sees an internal id.** It speaks only in source-system
terms, because a source id is the only identifier it can possibly know. The
runtime resolves source references to entity and principal ids.

**Payloads are verbatim.** Every record carries the source object unmodified, so
a field this SDK has never heard of is stored rather than dropped. Schema drift
is a warning and a stored payload, never data loss.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

# A connector-defined resume token, stored verbatim in sync_state.cursor as
# jsonb. Slack uses a message ts, Jira an updated-since plus a page token; the
# runtime never interprets it. An empty mapping means "from the beginning".
Cursor = Mapping[str, Any]

EMPTY_CURSOR: Cursor = {}

# Key a stream sets on its final cursor to mean "this pass is complete".
#
# Two kinds of stream need to be told apart. A watermark stream resumes and
# picks up what is new (Jira's updated-since). A listing stream pages through
# everything the source has and, when it reaches the end, has nowhere further to
# go: Slack has no incremental users.list. Its terminal cursor cannot mean
# "resume here" and must not mean "read it all again", so it means finished, and
# the runtime starts the next pass from an empty cursor.
#
# Resuming from a terminal cursor yields one empty final page, which is what
# keeps the resume contract exact.
DONE = "done"


def is_terminal(cursor: Cursor) -> bool:
    """True when this cursor marks a completed pass rather than a position."""
    return bool(cursor.get(DONE))


class SourceRef(BaseModel):
    """An object in the source system, in the source system's own terms."""

    model_config = ConfigDict(frozen=True)

    source_type: str = Field(min_length=1, description="e.g. 'slack.message', 'jira.issue'")
    source_id: str = Field(min_length=1, description="id in the source system")


class IdentityRecord(BaseModel):
    """A user or group. Becomes a principal.

    `member_of` carries source ids of the groups this principal belongs to, so
    a connector can express membership without knowing internal ids. The
    runtime turns them into principal_memberships once both sides exist.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["user", "group"]
    source_id: str = Field(min_length=1)
    email: str | None = None
    display_name: str | None = None
    member_of: tuple[str, ...] = ()
    payload: dict[str, Any] = Field(default_factory=dict)


class ContentRecord(BaseModel):
    """A message, issue, comment or document. Becomes a raw_record.

    `container` is what the object lives in: a message's channel, an issue's
    project, a comment's issue. It matters because source systems grant access
    to containers, not to individual objects. Slack shares a channel, not each
    of its ten thousand messages. Declaring the container lets a connector emit
    one ACL record per channel instead of one per message, and gives the
    resolver the containment edge it needs anyway.
    """

    model_config = ConfigDict(frozen=True)

    source_type: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    payload: dict[str, Any]
    container: SourceRef | None = None

    @property
    def ref(self) -> SourceRef:
        return SourceRef(source_type=self.source_type, source_id=self.source_id)


class AclRecord(BaseModel):
    """One principal's access to one object, as the source system states it.

    Deny by default is the runtime's rule, so there is no 'deny' record: a
    grant that stops being emitted stops existing. Revocation is the absence of
    a row, which is what makes the ACL fast-lane in P1-SYNC-4 a plain diff.
    """

    model_config = ConfigDict(frozen=True)

    target: SourceRef
    principal_source_id: str = Field(min_length=1)
    access: Literal["read"] = "read"


class Page(BaseModel):
    """One batch of records plus the cursor that resumes after them.

    The runtime commits a page's records and its cursor in one transaction, so
    a crash resumes at a page boundary and never mid-batch. That is the whole
    reason a stream yields pages rather than records: the cursor has to arrive
    with the data it corresponds to.
    """

    model_config = ConfigDict(frozen=True)

    records: tuple[Any, ...] = ()
    cursor: dict[str, Any] = Field(default_factory=dict)
    has_more: bool = False


class ConnectorError(Exception):
    """Base for every failure a connector is expected to raise."""


class RateLimitedError(ConnectorError):
    """The source asked us to slow down.

    Carries the source's own retry hint where it gives one. The runtime
    reschedules the job; it never spins and never crashes the worker.
    """

    def __init__(self, message: str = "rate limited", retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class TransientSourceError(ConnectorError):
    """A failure worth retrying: a 5xx, a timeout, a dropped connection."""


class PermanentSourceError(ConnectorError):
    """A failure retrying cannot fix: bad credentials, deleted resource, 4xx.

    Dead-letters immediately rather than burning the retry budget.
    """


class InverseCaptureError(ConnectorError):
    """The current state of a write-back target could not be read.

    CLAUDE.md rule 3: no inverse capture means the action fails. It does not
    mean the action executes without a rollback path.
    """


class ReadConnector(Protocol):
    """The three read streams.

    Each takes a cursor and yields pages. Contract:

    * `stream({})` yields every record the connector can see.
    * `stream(page.cursor)` yields exactly what follows that page, for a source
      that has not changed in between. Against a live source, re-delivering a
      record is allowed and safe, because the runtime upserts on
      (connector, source_type, source_id). Omitting one is never allowed.
    * The final page has `has_more=False`. Its cursor is what gets stored and
      handed back at the next sync.
    * A stream is a generator, so a connector holding a page of API results does
      not hold all of them.
    """

    kind: str
    schema_version: str

    def identities(self, cursor: Cursor) -> Iterator[Page]: ...

    def content(self, cursor: Cursor) -> Iterator[Page]: ...

    def acls(self, cursor: Cursor) -> Iterator[Page]: ...


class WritebackRequest(BaseModel):
    """An approved action, handed to the connector for execution."""

    model_config = ConfigDict(frozen=True)

    action_type: str = Field(min_length=1, description="e.g. 'jira.comment'")
    target: SourceRef | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class WritebackReceipt(BaseModel):
    """What the source system said in response."""

    model_config = ConfigDict(frozen=True)

    external_id: str | None = Field(
        default=None, description="id of the thing created, where the source returns one"
    )
    result: dict[str, Any] = Field(default_factory=dict)


class WritebackConnector(Protocol):
    """Optional fourth stream. A read-only connector simply does not implement it.

    Three methods rather than one, so that rule 3 is expressed by the shape of
    the interface: there is no way to execute without a separate, earlier call
    that returns the inverse.
    """

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        """Read the current state of the target, before changing it.

        Raise InverseCaptureError if it cannot be read. Do not return an empty
        inverse to get past this: an action with no rollback path must fail.
        """
        ...

    def execute(self, request: WritebackRequest) -> WritebackReceipt: ...

    def rollback(self, request: WritebackRequest, inverse: Mapping[str, Any]) -> None:
        """Put the target back the way `capture_inverse` found it."""
        ...


def perform_writeback(
    connector: WritebackConnector, request: WritebackRequest
) -> tuple[dict[str, Any], WritebackReceipt]:
    """Capture the inverse, then execute. Returns both.

    The runtime calls this rather than the connector's methods directly, so the
    ordering is not something each caller has to remember. If inverse capture
    raises, execute is never reached.
    """
    inverse = connector.capture_inverse(request)
    receipt = connector.execute(request)
    return inverse, receipt


def unknown_fields(payload: Mapping[str, Any], known: Iterable[str]) -> tuple[str, ...]:
    """Fields the source sent that this connector version does not model.

    Report these; do not act on them. The payload is stored verbatim either
    way, so drift costs a log line and never a record.
    """
    return tuple(sorted(set(payload) - set(known)))
