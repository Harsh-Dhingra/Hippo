"""The conformance harness.

Run any connector against this and find out whether it honours the contract in
sdk.py. It returns violations rather than raising, so it is usable from a
script, from CI, or from a contributor's editor, not only from pytest.

The checks exist because each one corresponds to a way a connector silently
loses or duplicates data. A connector whose cursor does not resume exactly
re-syncs the world every time or, worse, skips the records between the page it
crashed on and the page it resumes at. Neither failure is visible in a demo.

This grows into P3-SDK-1's published conformance package. Keep it dependency
free and keep the violations readable by someone who has not read this file.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sync.connectors.sdk import (
    READ_STREAMS,
    SDK_VERSION,
    AclRecord,
    Capabilities,
    ContentRecord,
    Cursor,
    IdentityRecord,
    InverseCaptureError,
    Page,
    ReadConnector,
    SourceRef,
    WritebackConnector,
    WritebackRequest,
    compatible,
    perform_writeback,
)

_EXPECTED_RECORD: dict[str, type[Any]] = {
    "identities": IdentityRecord,
    "content": ContentRecord,
    "acls": AclRecord,
}

MAX_PAGES = 10_000


@dataclass(frozen=True)
class Violation:
    """One broken promise, named so the fix is obvious."""

    check: str
    detail: str
    stream: str | None = None

    def __str__(self) -> str:
        where = f"[{self.stream}] " if self.stream else ""
        return f"{where}{self.check}: {self.detail}"


@dataclass(frozen=True)
class WritebackCase:
    """One write-back to exercise.

    `observe` returns whatever the connector considers the visible state of the
    target. The harness only compares it against itself, so any comparable
    value will do.
    """

    request: WritebackRequest
    observe: Callable[[], Any]


@dataclass
class _Report:
    violations: list[Violation] = field(default_factory=list)

    def fail(self, check: str, detail: str, stream: str | None = None) -> None:
        self.violations.append(Violation(check=check, detail=detail, stream=stream))


def _identity_of(record: Any) -> tuple[str, ...]:
    """A stable key for comparing records across runs."""
    if isinstance(record, IdentityRecord):
        return ("identity", record.kind, record.source_id)
    if isinstance(record, ContentRecord):
        return ("content", record.source_type, record.source_id)
    if isinstance(record, AclRecord):
        return (
            "acl",
            record.target.source_type,
            record.target.source_id,
            record.principal_source_id,
            record.access,
        )
    return ("unknown", repr(record))


def _drain(
    stream: Callable[[Cursor], Iterator[Page[Any]]], cursor: Cursor
) -> tuple[list[Page[Any]], Violation | None]:
    """Pull a stream to exhaustion, refusing to loop forever on a broken one."""
    pages: list[Page[Any]] = []
    iterator = stream(cursor)
    for page in iterator:
        pages.append(page)
        if len(pages) > MAX_PAGES:
            return pages, Violation(
                check="terminates",
                detail=(
                    f"stream produced more than {MAX_PAGES} pages; "
                    f"the cursor is probably not advancing"
                ),
            )
        if not page.has_more:
            # The runtime stops here, so anything the connector yields after
            # this point is data it will never deliver. Look for it.
            leftover = next(iterator, None)
            if leftover is not None:
                return pages, Violation(
                    check="has_more_is_accurate",
                    detail=(
                        f"page {len(pages) - 1} said has_more=False but the stream yielded "
                        f"another page with {len(leftover.records)} record(s); the runtime "
                        f"stops at the first has_more=False and would never see them"
                    ),
                )
            break
    return pages, None


def _check_read_stream(
    report: _Report, name: str, stream: Callable[[Cursor], Iterator[Page[Any]]]
) -> None:
    pages, runaway = _drain(stream, {})
    if runaway is not None:
        report.fail(runaway.check, runaway.detail, name)
        return

    if not pages:
        report.fail(
            "yields_at_least_one_page",
            "a stream must yield a page even when it has no records, so the runtime "
            "has a cursor to store",
            name,
        )
        return

    if pages[-1].has_more:
        report.fail(
            "terminates",
            "the last page still says has_more=True, so the runtime cannot tell it is done",
            name,
        )

    expected_type = _EXPECTED_RECORD[name]
    for index, page in enumerate(pages):
        try:
            json.dumps(page.cursor)
        except (TypeError, ValueError) as exc:
            report.fail(
                "cursor_is_json",
                f"page {index} cursor is not JSON-serialisable, so it cannot be stored "
                f"in sync_state.cursor: {exc}",
                name,
            )
        for record in page.records:
            if not isinstance(record, expected_type):
                report.fail(
                    "record_type",
                    f"page {index} yielded {type(record).__name__}, expected "
                    f"{expected_type.__name__}",
                    name,
                )

    _check_no_duplicates(report, name, pages)
    _check_determinism(report, name, stream, pages)
    _check_resume(report, name, stream, pages)


def _all_records(pages: Sequence[Page[Any]]) -> list[Any]:
    return [record for page in pages for record in page.records]


def _check_no_duplicates(report: _Report, name: str, pages: Sequence[Page[Any]]) -> None:
    keys = [_identity_of(record) for record in _all_records(pages)]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        report.fail(
            "no_duplicates_in_one_pass",
            f"a single full sync yielded the same record more than once: {duplicates[:5]}",
            name,
        )


def _check_determinism(
    report: _Report,
    name: str,
    stream: Callable[[Cursor], Iterator[Page[Any]]],
    first_pass: Sequence[Page[Any]],
) -> None:
    second_pass, runaway = _drain(stream, {})
    if runaway is not None:
        report.fail(runaway.check, runaway.detail, name)
        return

    before = [_identity_of(r) for r in _all_records(first_pass)]
    after = [_identity_of(r) for r in _all_records(second_pass)]
    if before != after:
        report.fail(
            "deterministic_from_empty_cursor",
            "two full syncs of an unchanged source produced different records; "
            "re-syncing must be repeatable",
            name,
        )


def _check_resume(
    report: _Report,
    name: str,
    stream: Callable[[Cursor], Iterator[Page[Any]]],
    pages: Sequence[Page[Any]],
) -> None:
    """The property that makes a crash survivable.

    Resuming from a page's cursor must yield exactly what came after that page.
    Yield too little and the records in between are lost forever; yield the
    whole world again and every restart re-syncs from scratch.
    """
    full = [_identity_of(r) for r in _all_records(pages)]
    consumed = 0

    for index, page in enumerate(pages):
        consumed += len(page.records)
        resumed, runaway = _drain(stream, page.cursor)
        if runaway is not None:
            report.fail(runaway.check, f"resuming from page {index}: {runaway.detail}", name)
            return

        got = [_identity_of(r) for r in _all_records(resumed)]
        expected = full[consumed:]
        if got != expected:
            report.fail(
                "cursor_resumes_exactly",
                f"resuming from the cursor of page {index} yielded {len(got)} record(s), "
                f"expected the {len(expected)} that follow it. "
                f"missing={sorted(set(expected) - set(got))[:3]} "
                f"unexpected={sorted(set(got) - set(expected))[:3]}",
                name,
            )
            return


def _check_acl_targets(report: _Report, connector: ReadConnector) -> None:
    """ACLs must point at something the connector also describes.

    A grant on an object nothing else mentions cannot be turned into a row: the
    runtime would have no entity to attach it to, and it would fail silently by
    granting nobody anything.
    """
    content_pages, _ = _drain(connector.content, {})
    identity_pages, _ = _drain(connector.identities, {})
    acl_pages, _ = _drain(connector.acls, {})

    known_objects: set[SourceRef] = set()
    for record in _all_records(content_pages):
        if isinstance(record, ContentRecord):
            known_objects.add(record.ref)
            if record.container is not None:
                known_objects.add(record.container)

    known_principals = {
        record.source_id
        for record in _all_records(identity_pages)
        if isinstance(record, IdentityRecord)
    }

    for record in _all_records(acl_pages):
        if not isinstance(record, AclRecord):
            continue
        if record.target not in known_objects:
            report.fail(
                "acl_targets_a_known_object",
                f"grant on {record.target.source_type}:{record.target.source_id}, which the "
                f"content stream neither emits nor names as a container",
                "acls",
            )
        if record.principal_source_id not in known_principals:
            report.fail(
                "acl_names_a_known_principal",
                f"grant to principal {record.principal_source_id!r}, which the identities "
                f"stream never emits",
                "acls",
            )


def _check_writeback(
    report: _Report, connector: WritebackConnector, cases: Sequence[WritebackCase]
) -> None:
    for index, case in enumerate(cases):
        before = case.observe()

        try:
            inverse, receipt = perform_writeback(connector, case.request)
        except InverseCaptureError as exc:
            report.fail(
                "inverse_capture_succeeds_for_a_valid_target",
                f"case {index}: {exc}",
                "writeback",
            )
            continue

        after = case.observe()
        if after == before:
            report.fail(
                "execute_changes_something",
                f"case {index}: state is unchanged after execute, so the harness cannot "
                f"tell whether rollback works",
                "writeback",
            )
            continue

        if not receipt.external_id and not receipt.result:
            report.fail(
                "execute_returns_a_receipt",
                f"case {index}: the receipt carries neither an external id nor a result, "
                f"so nothing links the action row to what the source actually did",
                "writeback",
            )

        try:
            connector.rollback(case.request, inverse)
        except Exception as exc:
            report.fail("rollback_succeeds", f"case {index}: {exc!r}", "writeback")
            continue

        restored = case.observe()
        if restored != before:
            report.fail(
                "rollback_restores_the_captured_state",
                f"case {index}: after rollback the target is {restored!r}, "
                f"but capture_inverse saw {before!r}",
                "writeback",
            )


def _check_inverse_is_required(
    report: _Report, connector: WritebackConnector, impossible: WritebackRequest
) -> None:
    """A target whose state cannot be read must fail, not execute blind."""
    try:
        perform_writeback(connector, impossible)
    except InverseCaptureError:
        return
    except Exception as exc:
        report.fail(
            "unreadable_target_raises_inverse_capture_failed",
            f"raised {type(exc).__name__} instead of InverseCaptureError",
            "writeback",
        )
        return
    report.fail(
        "unreadable_target_raises_inverse_capture_failed",
        "an action whose inverse cannot be captured was executed anyway",
        "writeback",
    )


def _check_capabilities(
    report: _Report,
    connector: ReadConnector,
    *,
    writeback: WritebackConnector | None,
) -> None:
    """A declaration that does not match the object is worse than none.

    The runtime trusts `capabilities()` — it is how it decides whether to route
    an action here at all — so a connector that claims write-back and cannot do
    it produces an approved action that fails at execution, after a person has
    read it and clicked. That is the failure this check exists to prevent.
    """
    declared = getattr(connector, "capabilities", None)
    if declared is None:
        report.fail(
            "declares_capabilities",
            "connector has no capabilities(); the runtime cannot tell what it supports "
            "without one, and guessing is how a read-only connector gets sent an action",
        )
        return

    try:
        capabilities = declared()
    except Exception as exc:
        report.fail("declares_capabilities", f"capabilities() raised {type(exc).__name__}: {exc}")
        return

    if not isinstance(capabilities, Capabilities):
        report.fail(
            "declares_capabilities",
            f"capabilities() returned {type(capabilities).__name__}, expected Capabilities",
        )
        return

    if capabilities.kind != getattr(connector, "kind", ""):
        report.fail(
            "capabilities_match_connector",
            f"capabilities().kind is {capabilities.kind!r} but connector.kind is "
            f"{getattr(connector, 'kind', '')!r}",
        )

    if not compatible(capabilities.sdk_version):
        report.fail(
            "sdk_version_supported",
            f"built against SDK {capabilities.sdk_version}, this runtime is {SDK_VERSION}",
        )

    missing_streams = sorted(set(capabilities.streams) - set(READ_STREAMS))
    if missing_streams:
        report.fail(
            "declares_known_streams",
            f"declares streams that do not exist: {', '.join(missing_streams)}",
        )

    if capabilities.supports_writeback:
        # Whichever object actually writes. The SDK keeps ReadConnector and
        # WritebackConnector as separate protocols on purpose, so a connector
        # may put the write path in its own class — and checking the read half
        # would then report a violation that is not there.
        performer: Any = writeback if writeback is not None else connector
        methods = ("capture_inverse", "execute", "rollback")
        present = [name for name in methods if hasattr(performer, name)]
        absent = [name for name in methods if not hasattr(performer, name)]

        # None of the three, with nothing passed as `writeback`, is the split
        # shape — the write half lives elsewhere and was not handed over. That
        # is indistinguishable from a lie here, so it is not reported here; the
        # registry catches it when it builds the writer, which is the path the
        # runtime actually takes.
        if absent and (present or writeback is not None):
            report.fail(
                "writeback_is_implemented",
                f"declares {len(capabilities.actions)} action(s) but "
                f"{type(performer).__name__} is missing {', '.join(absent)}; "
                "an approved action would fail at execution",
            )
    elif writeback is not None:
        report.fail(
            "writeback_is_declared",
            "a write-back connector was supplied but capabilities() declares no actions, "
            "so the runtime would never route an action to it",
        )

    for action in capabilities.actions:
        if not action.targets:
            report.fail(
                "actions_name_their_targets",
                f"{action.action_type} names no target source types, so a proposal could "
                "aim it at anything retrieved",
            )
        if getattr(action.payload_model, "model_config", {}).get("extra") != "forbid":
            report.fail(
                "action_payloads_forbid_extra",
                f"{action.action_type}'s payload model allows extra fields; a proposal "
                "could carry something this connector passes through unexamined",
            )


def check_connector(
    connector: ReadConnector,
    *,
    writeback: WritebackConnector | None = None,
    writeback_cases: Sequence[WritebackCase] = (),
    uncapturable_request: WritebackRequest | None = None,
) -> tuple[Violation, ...]:
    """Run every applicable check. Empty result means the connector conforms."""
    report = _Report()

    if not getattr(connector, "kind", ""):
        report.fail("declares_kind", "connector.kind is empty; it names the connector")
    if not getattr(connector, "schema_version", ""):
        report.fail(
            "declares_schema_version",
            "connector.schema_version is empty; drift detection needs a baseline",
        )

    _check_capabilities(report, connector, writeback=writeback)

    for name in READ_STREAMS:
        _check_read_stream(report, name, getattr(connector, name))

    _check_acl_targets(report, connector)

    if writeback is not None:
        _check_writeback(report, writeback, writeback_cases)
        if uncapturable_request is not None:
            _check_inverse_is_required(report, writeback, uncapturable_request)

    return tuple(report.violations)


def assert_conforms(
    connector: ReadConnector,
    *,
    writeback: WritebackConnector | None = None,
    writeback_cases: Sequence[WritebackCase] = (),
    uncapturable_request: WritebackRequest | None = None,
) -> None:
    """check_connector, as an assertion with a readable report."""
    violations = check_connector(
        connector,
        writeback=writeback,
        writeback_cases=writeback_cases,
        uncapturable_request=uncapturable_request,
    )
    if violations:
        report = "\n  ".join(str(v) for v in violations)
        kind = getattr(connector, "kind", type(connector).__name__)
        raise AssertionError(f"connector {kind!r} does not conform:\n  {report}")
