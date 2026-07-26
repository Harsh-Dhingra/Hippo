"""The conformance harness.

Half of this file is the done-condition: the mock connector passes. The other
half is the part that gives the first half meaning. A harness that accepts
everything is worse than no harness, because it certifies broken connectors, so
every check gets a connector built to break exactly it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from sync.connectors import harness
from sync.connectors.harness import (
    WritebackCase,
    assert_conforms,
    check_connector,
)
from sync.connectors.sdk import (
    AclRecord,
    ContentRecord,
    Cursor,
    IdentityRecord,
    Page,
    SourceRef,
    WritebackReceipt,
    WritebackRequest,
)
from tests.mock_connector import MockConnector, MockWriteback

TICKET = SourceRef(source_type="mock.ticket", source_id="TICKET-1")
GHOST = SourceRef(source_type="mock.ticket", source_id="NOPE")


def failed_checks(connector: Any, **kwargs: Any) -> set[str]:
    return {violation.check for violation in check_connector(connector, **kwargs)}


def comment(target: SourceRef = TICKET) -> WritebackRequest:
    return WritebackRequest(
        action_type=MockWriteback.COMMENT, target=target, payload={"body": "from the harness"}
    )


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------


def test_the_mock_connector_conforms() -> None:
    """P1-SYNC-1's done-condition."""
    source = MockWriteback()

    assert_conforms(
        MockConnector(),
        writeback=source,
        writeback_cases=[WritebackCase(request=comment(), observe=lambda: source.comments(TICKET))],
        uncapturable_request=comment(GHOST),
    )


@pytest.mark.parametrize("page_size", [1, 2, 3, 5, 50])
def test_the_mock_connector_conforms_at_every_page_size(page_size: int) -> None:
    """Page boundaries are where cursor bugs live: one record per page, exactly
    one page, and everything between."""
    assert_conforms(MockConnector(page_size=page_size))


def test_a_conforming_connector_produces_no_violations() -> None:
    assert check_connector(MockConnector()) == ()


def test_fixtures_describe_a_world_the_permission_tests_can_use() -> None:
    """The fixture corpus is the input to P1-SYNC-2 and P1-RES-1, so its shape
    is part of this fragment's output."""
    connector = MockConnector()

    identities = [r for page in connector.identities({}) for r in page.records]
    content = [r for page in connector.content({}) for r in page.records]
    acls = [r for page in connector.acls({}) for r in page.records]

    assert {r.source_id for r in identities} == {
        "G-ENG",
        "G-LEADS",
        "U-ALICE",
        "U-BOB",
        "U-CAROL",
    }
    assert any(r.container is not None for r in content), "messages must name their channel"
    assert {r.principal_source_id for r in acls} == {"G-ENG", "U-CAROL", "U-BOB"}
    private = [r for r in acls if r.target.source_id == "C-DEALS"]
    assert [r.principal_source_id for r in private] == ["U-BOB"], (
        "the private channel is the one the filtered-path demo depends on"
    )


# ---------------------------------------------------------------------------
# Connectors built to break one check each.
# ---------------------------------------------------------------------------


class YieldsNoPages(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        return
        yield  # pragma: no cover - unreachable, makes this a generator


class NeverFinishes(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        while True:
            yield Page(records=(), cursor={"offset": 0}, has_more=True)


class ClaimsMoreThanItHas(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield Page(records=(), cursor={"offset": 0}, has_more=True)


class LiesAboutHasMore(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield Page(records=(), cursor={"offset": 0}, has_more=False)
        yield Page(records=(), cursor={"offset": 1}, has_more=False)


class IgnoresTheCursor(MockConnector):
    """Restarts from the beginning every time, so a resumed sync re-reads the
    entire source forever."""

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield from super().content({})


class LosesRecordsOnResume(MockConnector):
    """The dangerous one: resuming skips ahead, and the records in between are
    never delivered by any sync."""

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        if cursor.get("offset"):
            yield Page(records=(), cursor=dict(cursor), has_more=False)
            return
        yield from super().content(cursor)


class RepeatsItself(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        record = ContentRecord(source_type="mock.message", source_id="M-1", payload={})
        yield Page(records=(record, record), cursor={"offset": 1}, has_more=False)


class ChangesItsMind(MockConnector):
    def __init__(self) -> None:
        super().__init__()
        self._runs = 0

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        self._runs += 1
        record = ContentRecord(source_type="mock.message", source_id=f"M-{self._runs}", payload={})
        yield Page(records=(record,), cursor={"offset": 1}, has_more=False)


class UnstorableCursor(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield Page(records=(), cursor={"seen": {"a", "set"}}, has_more=False)


class WrongRecordType(MockConnector):
    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield Page(
            records=(IdentityRecord(kind="user", source_id="U-ALICE"),),
            cursor={"offset": 1},
            has_more=False,
        )


class GrantsOnAnUnknownObject(MockConnector):
    def acls(self, cursor: Cursor) -> Iterator[Page[Any]]:
        record = AclRecord(
            target=SourceRef(source_type="mock.channel", source_id="C-GHOST"),
            principal_source_id="U-ALICE",
        )
        yield Page(records=(record,), cursor={"offset": 1}, has_more=False)


class GrantsToAnUnknownPrincipal(MockConnector):
    def acls(self, cursor: Cursor) -> Iterator[Page[Any]]:
        record = AclRecord(
            target=SourceRef(source_type="mock.channel", source_id="C-GENERAL"),
            principal_source_id="U-NOBODY",
        )
        yield Page(records=(record,), cursor={"offset": 1}, has_more=False)


class YieldsSomethingElseEntirely(MockConnector):
    """Not a record at all. The harness must say so rather than crash on it."""

    def acls(self, cursor: Cursor) -> Iterator[Page[Any]]:
        yield Page(records=("just a string",), cursor={"offset": 1}, has_more=False)


class LoopsOnTheSecondPass(MockConnector):
    """Terminates once, then does not. Caught by the determinism re-run."""

    def __init__(self) -> None:
        super().__init__()
        self._passes = 0

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        self._passes += 1
        if self._passes >= 2:
            while True:
                yield Page(records=(), cursor={"offset": 0}, has_more=True)
        else:
            yield from super().content(cursor)


class LoopsOnResume(MockConnector):
    """Terminates from an empty cursor and never from a resumed one, which is
    the shape a real paging bug takes."""

    def content(self, cursor: Cursor) -> Iterator[Page[Any]]:
        if cursor.get("offset"):
            while True:
                yield Page(records=(), cursor=dict(cursor), has_more=True)
        else:
            yield from super().content(cursor)


class Anonymous(MockConnector):
    kind = ""
    schema_version = ""


@pytest.mark.parametrize(
    ("connector_class", "expected_check"),
    [
        (YieldsNoPages, "yields_at_least_one_page"),
        (ClaimsMoreThanItHas, "terminates"),
        (LiesAboutHasMore, "has_more_is_accurate"),
        (IgnoresTheCursor, "cursor_resumes_exactly"),
        (LosesRecordsOnResume, "cursor_resumes_exactly"),
        (RepeatsItself, "no_duplicates_in_one_pass"),
        (ChangesItsMind, "deterministic_from_empty_cursor"),
        (UnstorableCursor, "cursor_is_json"),
        (WrongRecordType, "record_type"),
        (GrantsOnAnUnknownObject, "acl_targets_a_known_object"),
        (GrantsToAnUnknownPrincipal, "acl_names_a_known_principal"),
    ],
)
def test_the_harness_catches(connector_class: type[MockConnector], expected_check: str) -> None:
    assert expected_check in failed_checks(connector_class())


def test_the_harness_catches_a_record_that_is_not_a_record() -> None:
    """A stream can yield anything; the report must name it, not raise."""
    assert "record_type" in failed_checks(YieldsSomethingElseEntirely())


@pytest.mark.parametrize(
    "connector_class",
    [NeverFinishes, LoopsOnTheSecondPass, LoopsOnResume],
)
def test_the_harness_catches_a_stream_that_never_terminates(
    monkeypatch: pytest.MonkeyPatch, connector_class: type[MockConnector]
) -> None:
    """Whether the loop shows up on the first pass, the determinism re-run, or
    only once a cursor is handed back."""
    monkeypatch.setattr(harness, "MAX_PAGES", 20)
    assert "terminates" in failed_checks(connector_class())


def test_the_harness_catches_an_unnamed_connector() -> None:
    failures = failed_checks(Anonymous())
    assert "declares_kind" in failures
    assert "declares_schema_version" in failures


def test_a_violation_reads_as_a_sentence() -> None:
    (violation,) = [v for v in check_connector(LiesAboutHasMore()) if v.stream == "content"][:1]
    assert str(violation).startswith("[content] has_more_is_accurate:")


def test_assert_conforms_raises_with_every_violation_listed() -> None:
    with pytest.raises(AssertionError) as caught:
        assert_conforms(GrantsToAnUnknownPrincipal())

    message = str(caught.value)
    assert "does not conform" in message
    assert "acl_names_a_known_principal" in message
    assert "U-NOBODY" in message


# ---------------------------------------------------------------------------
# Write-back checks.
# ---------------------------------------------------------------------------


class ExecutesWithoutAnInverse(MockWriteback):
    """Returns an empty inverse instead of failing. The exact shortcut rule 3
    exists to forbid: the action runs, and nothing can undo it."""

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        self.calls.append("capture_inverse")
        key = self._key(request.target)
        self.tickets.setdefault(key, [])
        return {}


class RollbackDoesNothing(MockWriteback):
    def rollback(self, request: WritebackRequest, inverse: Any) -> None:
        self.calls.append("rollback")


class ExecuteChangesNothing(MockWriteback):
    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        self.calls.append("execute")
        return WritebackReceipt(external_id="noop")


class ReturnsAnEmptyReceipt(MockWriteback):
    """Does the work and says nothing about it, so the action row has no link
    back to what the source actually did."""

    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        super().execute(request)
        return WritebackReceipt()


def test_the_harness_catches_an_action_executed_without_an_inverse() -> None:
    source = ExecutesWithoutAnInverse()

    failures = failed_checks(MockConnector(), writeback=source, uncapturable_request=comment(GHOST))

    assert "unreadable_target_raises_inverse_capture_failed" in failures


def test_the_harness_catches_a_rollback_that_does_not_restore() -> None:
    source = RollbackDoesNothing()

    failures = failed_checks(
        MockConnector(),
        writeback=source,
        writeback_cases=[WritebackCase(request=comment(), observe=lambda: source.comments(TICKET))],
    )

    assert "rollback_restores_the_captured_state" in failures


def test_the_harness_catches_an_execute_that_does_nothing() -> None:
    source = ExecuteChangesNothing()

    failures = failed_checks(
        MockConnector(),
        writeback=source,
        writeback_cases=[WritebackCase(request=comment(), observe=lambda: source.comments(TICKET))],
    )

    assert "execute_changes_something" in failures


def test_the_harness_catches_an_execute_that_reports_nothing() -> None:
    source = ReturnsAnEmptyReceipt()

    failures = failed_checks(
        MockConnector(),
        writeback=source,
        writeback_cases=[WritebackCase(request=comment(), observe=lambda: source.comments(TICKET))],
    )

    assert "execute_returns_a_receipt" in failures


def test_the_harness_catches_a_rollback_that_raises() -> None:
    source = MockWriteback()
    case = WritebackCase(request=comment(), observe=lambda: source.comments(TICKET))

    def explode(request: WritebackRequest, inverse: Any) -> None:
        raise RuntimeError("source rejected the rollback")

    source.rollback = explode  # type: ignore[method-assign]

    assert "rollback_succeeds" in failed_checks(
        MockConnector(), writeback=source, writeback_cases=[case]
    )


def test_the_harness_reports_a_target_it_cannot_capture() -> None:
    source = MockWriteback()
    case = WritebackCase(request=comment(GHOST), observe=lambda: source.comments(GHOST))

    assert "inverse_capture_succeeds_for_a_valid_target" in failed_checks(
        MockConnector(), writeback=source, writeback_cases=[case]
    )


def test_the_harness_flags_the_wrong_exception_type() -> None:
    """A connector that raises its own error for an unreadable target is still
    wrong: the runtime keys on InverseCaptureError to refuse execution."""
    source = MockWriteback()

    def wrong_error(request: WritebackRequest) -> dict[str, Any]:
        raise ValueError("could not read")

    source.capture_inverse = wrong_error  # type: ignore[method-assign]

    assert "unreadable_target_raises_inverse_capture_failed" in failed_checks(
        MockConnector(), writeback=source, uncapturable_request=comment(GHOST)
    )
