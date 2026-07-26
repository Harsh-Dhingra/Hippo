"""A connector built only from fixture files.

Two jobs. It is what P1-SYNC-1's done-condition runs the harness against, and
it is the worked example a contributor reads before writing a real one, so it
stays deliberately boring: no network, no cleverness, offset pagination.

The write-back half models a source system in memory, which is enough to prove
the inverse-capture contract without a Jira account.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sync.connectors.sdk import (
    AclRecord,
    ActionDefinition,
    Capabilities,
    ContentRecord,
    Cursor,
    IdentityRecord,
    InverseCaptureError,
    Page,
    PermanentSourceError,
    SourceRef,
    WritebackReceipt,
    WritebackRequest,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "mock"

COMMENT_ACTION = "mock.comment"


class MockCommentPayload(BaseModel):
    """extra="forbid" is part of the contract, not a preference: it is what
    stops a proposal carrying a field the connector would pass through."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str = Field(min_length=1, max_length=4000)


ACTIONS = (
    ActionDefinition(
        action_type=COMMENT_ACTION,
        description="Add a comment to a mock ticket.",
        targets=frozenset({"mock.ticket"}),
        payload_schema='{"body": "the comment text"}',
        payload_model=MockCommentPayload,
    ),
)


def _load(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):  # pragma: no cover - fixture authoring error
        msg = f"{path} must contain a JSON array"
        raise ValueError(msg)
    return [dict(item) for item in data]


class MockConnector:
    """Reads three fixture files and pages through them.

    The cursor is an offset. A real connector's cursor is whatever its source
    understands, a Slack ts or a Jira updated-since token; the runtime stores
    it as opaque jsonb either way.
    """

    kind = "mock"
    schema_version = "2026-07-01"

    def capabilities(self) -> Capabilities:
        """Declared, like any connector's, so the worked example shows the
        shape a contributor should copy.

        The actions are declared here even though `MockWriteback` performs
        them: capabilities describe the connector as the runtime sees it, and
        the runtime decides whether to route an action from this and nothing
        else. A split implementation that declared nothing here would never be
        sent one.
        """
        return Capabilities(
            kind=self.kind,
            schema_version=self.schema_version,
            actions=ACTIONS,
        )

    def __init__(self, root: Path = FIXTURES, *, page_size: int = 2) -> None:
        if page_size < 1:
            msg = f"page_size must be >= 1, got {page_size}"
            raise ValueError(msg)
        self._page_size = page_size
        self._identities = [IdentityRecord(**row) for row in _load(root / "identities.json")]
        self._content = [ContentRecord(**row) for row in _load(root / "content.json")]
        self._acls = [AclRecord(**row) for row in _load(root / "acls.json")]

    def _paginate(self, records: Sequence[Any], cursor: Cursor) -> Iterator[Page[Any]]:
        offset = int(cursor.get("offset", 0))
        while True:
            batch = tuple(records[offset : offset + self._page_size])
            offset += len(batch)
            has_more = offset < len(records)
            yield Page(records=batch, cursor={"offset": offset}, has_more=has_more)
            if not has_more:
                return

    def identities(self, cursor: Cursor) -> Iterator[Page[IdentityRecord]]:
        yield from self._paginate(self._identities, cursor)

    def content(self, cursor: Cursor) -> Iterator[Page[ContentRecord]]:
        yield from self._paginate(self._content, cursor)

    def acls(self, cursor: Cursor) -> Iterator[Page[AclRecord]]:
        yield from self._paginate(self._acls, cursor)


class MockWriteback:
    """An in-memory source system that accepts comments and can be put back.

    Deliberately stores state in a way `capture_inverse` can read and
    `rollback` can restore wholesale, which is the shape a real connector
    should aim for: capture enough to reconstruct, not a diff.
    """

    COMMENT = COMMENT_ACTION

    def __init__(self) -> None:
        self.tickets: dict[tuple[str, str], list[str]] = {
            ("mock.ticket", "TICKET-1"): ["existing comment"],
            ("mock.ticket", "TICKET-2"): [],
        }
        self.calls: list[str] = []

    def _key(self, target: SourceRef | None) -> tuple[str, str]:
        if target is None:
            msg = "write-back requires a target"
            raise InverseCaptureError(msg)
        return (target.source_type, target.source_id)

    def comments(self, target: SourceRef) -> tuple[str, ...]:
        return tuple(self.tickets.get(self._key(target), ()))

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        self.calls.append("capture_inverse")
        key = self._key(request.target)
        if key not in self.tickets:
            msg = f"cannot read current state of {key[0]}:{key[1]}"
            raise InverseCaptureError(msg)
        return {"comments": list(self.tickets[key])}

    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        self.calls.append("execute")
        if request.action_type != self.COMMENT:
            msg = f"unsupported action_type {request.action_type!r}"
            raise PermanentSourceError(msg)
        key = self._key(request.target)
        body = str(request.payload.get("body", ""))
        self.tickets[key].append(body)
        return WritebackReceipt(
            external_id=f"{key[1]}#{len(self.tickets[key])}",
            result={"body": body},
        )

    def rollback(self, request: WritebackRequest, inverse: Mapping[str, Any]) -> None:
        self.calls.append("rollback")
        key = self._key(request.target)
        restored = inverse.get("comments")
        if not isinstance(restored, list):
            msg = f"inverse payload is not restorable: {inverse!r}"
            raise PermanentSourceError(msg)
        self.tickets[key] = list(restored)
