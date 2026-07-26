"""The connector contract itself: models, errors, and the write-back ordering."""

from typing import Any

import pytest
from pydantic import ValidationError

from sync.connectors.sdk import (
    AclRecord,
    ConnectorError,
    ContentRecord,
    IdentityRecord,
    InverseCaptureError,
    Page,
    PermanentSourceError,
    RateLimitedError,
    SourceRef,
    TransientSourceError,
    WritebackReceipt,
    WritebackRequest,
    perform_writeback,
    unknown_fields,
)
from tests.mock_connector import MockWriteback

TICKET = SourceRef(source_type="mock.ticket", source_id="TICKET-1")


# ---------------------------------------------------------------------------
# Records.
# ---------------------------------------------------------------------------


def test_records_are_immutable() -> None:
    """A record is a fact the source stated. Nothing downstream edits it."""
    record = ContentRecord(source_type="mock.message", source_id="M-1", payload={"text": "hi"})

    with pytest.raises(ValidationError):
        record.source_id = "M-2"


def test_content_record_exposes_its_own_ref() -> None:
    record = ContentRecord(source_type="mock.message", source_id="M-1", payload={})
    assert record.ref == SourceRef(source_type="mock.message", source_id="M-1")


def test_a_content_record_keeps_fields_the_sdk_does_not_model() -> None:
    """Drift never loses data: the payload is stored as the source sent it."""
    payload = {"text": "hi", "a_field_invented_next_year": {"nested": True}}
    record = ContentRecord(source_type="mock.message", source_id="M-1", payload=payload)

    assert record.payload == payload


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_type": "", "source_id": "M-1", "payload": {}},
        {"source_type": "mock.message", "source_id": "", "payload": {}},
    ],
)
def test_empty_source_identifiers_are_rejected(kwargs: dict[str, Any]) -> None:
    """An empty id would upsert every record onto the same row."""
    with pytest.raises(ValidationError):
        ContentRecord(**kwargs)


def test_identity_membership_is_expressed_in_source_ids() -> None:
    """A connector cannot know internal ids, so it names groups the only way
    it can."""
    record = IdentityRecord(kind="user", source_id="U-ALICE", member_of=("G-ENG",))
    assert record.member_of == ("G-ENG",)


def test_identity_kind_is_constrained() -> None:
    with pytest.raises(ValidationError):
        IdentityRecord(kind="service", source_id="U-1")  # type: ignore[arg-type]


def test_acl_access_defaults_to_read() -> None:
    """v0 grants read. Anything else needs a schema change, not a string."""
    record = AclRecord(target=TICKET, principal_source_id="U-ALICE")
    assert record.access == "read"


def test_acl_access_is_constrained() -> None:
    with pytest.raises(ValidationError):
        AclRecord(target=TICKET, principal_source_id="U-1", access="write")  # type: ignore[arg-type]


def test_a_page_defaults_to_empty_and_final() -> None:
    """An empty stream still yields a page, so the runtime gets a cursor."""
    page: Page[Any] = Page()
    assert page.records == ()
    assert page.cursor == {}
    assert page.has_more is False


# ---------------------------------------------------------------------------
# Write-back: rule 3, expressed by the shape of the interface.
# ---------------------------------------------------------------------------


def test_perform_writeback_captures_the_inverse_before_executing() -> None:
    source = MockWriteback()
    request = WritebackRequest(
        action_type=MockWriteback.COMMENT, target=TICKET, payload={"body": "added"}
    )

    inverse, receipt = perform_writeback(source, request)

    assert source.calls == ["capture_inverse", "execute"]
    assert inverse == {"comments": ["existing comment"]}
    assert isinstance(receipt, WritebackReceipt)
    assert source.comments(TICKET) == ("existing comment", "added")


def test_a_failed_inverse_capture_prevents_execution() -> None:
    """The rule that matters most here: no inverse means the action fails, not
    that it executes without a rollback path."""
    source = MockWriteback()
    request = WritebackRequest(
        action_type=MockWriteback.COMMENT,
        target=SourceRef(source_type="mock.ticket", source_id="DOES-NOT-EXIST"),
        payload={"body": "added"},
    )

    with pytest.raises(InverseCaptureError):
        perform_writeback(source, request)

    assert source.calls == ["capture_inverse"], "execute must never have been reached"


def test_a_missing_target_fails_inverse_capture() -> None:
    source = MockWriteback()
    request = WritebackRequest(action_type=MockWriteback.COMMENT, payload={"body": "x"})

    with pytest.raises(InverseCaptureError):
        perform_writeback(source, request)


def test_rollback_restores_what_capture_inverse_saw() -> None:
    source = MockWriteback()
    request = WritebackRequest(
        action_type=MockWriteback.COMMENT, target=TICKET, payload={"body": "added"}
    )
    before = source.comments(TICKET)

    inverse, _ = perform_writeback(source, request)
    source.rollback(request, inverse)

    assert source.comments(TICKET) == before


def test_rollback_works_from_an_empty_starting_state() -> None:
    """The awkward case: rolling back to nothing, not to something."""
    source = MockWriteback()
    empty = SourceRef(source_type="mock.ticket", source_id="TICKET-2")
    request = WritebackRequest(
        action_type=MockWriteback.COMMENT, target=empty, payload={"body": "first"}
    )

    inverse, _ = perform_writeback(source, request)
    assert source.comments(empty) == ("first",)

    source.rollback(request, inverse)
    assert source.comments(empty) == ()


# ---------------------------------------------------------------------------
# Errors.
# ---------------------------------------------------------------------------


def test_rate_limited_carries_the_sources_own_retry_hint() -> None:
    """The runtime reschedules on this rather than guessing a backoff."""
    error = RateLimitedError("slow down", retry_after=30.0)

    assert error.retry_after == 30.0
    assert str(error) == "slow down"


def test_rate_limited_without_a_hint_is_still_valid() -> None:
    """Not every source tells you how long to wait."""
    assert RateLimitedError().retry_after is None


@pytest.mark.parametrize(
    "error_class",
    [RateLimitedError, TransientSourceError, PermanentSourceError, InverseCaptureError],
)
def test_every_connector_error_is_catchable_as_one(error_class: type[Exception]) -> None:
    """The runtime catches ConnectorError to tell a source failure from a bug
    in the connector itself."""
    assert issubclass(error_class, ConnectorError)


# ---------------------------------------------------------------------------
# Drift.
# ---------------------------------------------------------------------------


def test_unknown_fields_reports_what_the_connector_does_not_model() -> None:
    payload = {"id": "1", "text": "hi", "brand_new": True, "also_new": 2}

    assert unknown_fields(payload, ["id", "text"]) == ("also_new", "brand_new")


def test_unknown_fields_is_empty_when_the_schema_matches() -> None:
    assert unknown_fields({"id": "1"}, ["id", "text"]) == ()
