"""The Jira connector, against recorded responses."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from sync.connectors.harness import assert_conforms, check_connector
from sync.connectors.jira import FixtureTransport, HttpTransport, JiraConnector
from sync.connectors.jira.connector import COMMENT, ISSUE, PROJECT
from sync.connectors.sdk import (
    PermanentSourceError,
    RateLimitedError,
    TransientSourceError,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"


@pytest.fixture
def jira() -> JiraConnector:
    return JiraConnector(FixtureTransport(FIXTURES))


def records(pages: Any) -> list[Any]:
    return [record for page in pages for record in page.records]


# ---------------------------------------------------------------------------
# Conformance. The SDK's second implementation.
# ---------------------------------------------------------------------------


def test_the_jira_connector_conforms(jira: JiraConnector) -> None:
    assert_conforms(jira)


@pytest.mark.parametrize("page_size", [1, 2, 3, 50])
def test_conformance_holds_at_every_page_size(page_size: int) -> None:
    """Jira pages by offset against a total, which breaks differently from
    Slack's opaque cursor. Every boundary is worth checking."""
    assert check_connector(JiraConnector(FixtureTransport(FIXTURES), page_size=page_size)) == ()


# ---------------------------------------------------------------------------
# Identities.
# ---------------------------------------------------------------------------


def test_users_and_groups_both_become_identities(jira: JiraConnector) -> None:
    identities = records(jira.identities({}))

    assert {r.source_id for r in identities if r.kind == "group"} == {"g-dev", "g-support"}
    assert {r.source_id for r in identities if r.kind == "user"} == {
        "u-alice",
        "u-bob",
        "u-carol",
    }


def test_inactive_and_app_accounts_are_skipped(jira: JiraConnector) -> None:
    ids = {r.source_id for r in records(jira.identities({}))}

    assert "u-departed" not in ids
    assert "app-automation" not in ids


def test_membership_is_recorded_from_the_users_side(jira: JiraConnector) -> None:
    """Jira states membership from the group; the SDK records it on the user.
    Each user must still be emitted exactly once."""
    users = {r.source_id: r for r in records(jira.identities({})) if r.kind == "user"}

    assert users["u-alice"].member_of == ("g-dev",)
    assert users["u-carol"].member_of == ("g-support",)


def test_a_user_is_emitted_once_regardless_of_group_count(jira: JiraConnector) -> None:
    ids = [r.source_id for r in records(jira.identities({})) if r.kind == "user"]

    assert len(ids) == len(set(ids))


def test_emails_line_up_with_slack_for_cross_system_merge(jira: JiraConnector) -> None:
    """P1-RES-2 merges a person across systems by email, so the two fixture
    corpora deliberately describe the same three people."""
    emails = {r.email for r in records(jira.identities({})) if r.kind == "user"}

    assert emails == {"alice@example.com", "bob@example.com", "carol@example.com"}


def test_identity_payloads_keep_unmodelled_fields(jira: JiraConnector) -> None:
    bob = next(r for r in records(jira.identities({})) if r.source_id == "u-bob")

    assert bob.payload["an_unmodelled_field"] == "kept verbatim"


# ---------------------------------------------------------------------------
# Content, and two levels of containment.
# ---------------------------------------------------------------------------


def test_projects_issues_and_comments_are_all_content(jira: JiraConnector) -> None:
    by_type: dict[str, set[str]] = {}
    for record in records(jira.content({})):
        by_type.setdefault(record.source_type, set()).add(record.source_id)

    assert by_type[PROJECT] == {"ACME", "PUB"}
    assert by_type[ISSUE] == {"ACME-1", "ACME-2", "PUB-1"}
    assert by_type[COMMENT] == {"ACME-1:10100", "ACME-1:10101", "PUB-1:10200"}


def test_an_issue_names_its_project_as_container(jira: JiraConnector) -> None:
    issues = [r for r in records(jira.content({})) if r.source_type == ISSUE]

    containers = {r.source_id: r.container for r in issues}
    assert containers["ACME-1"] is not None
    assert containers["ACME-1"].source_id == "ACME"
    assert containers["PUB-1"] is not None
    assert containers["PUB-1"].source_id == "PUB"


def test_a_comment_names_its_issue_as_container(jira: JiraConnector) -> None:
    """The second level. A project grant reaches a comment through its issue."""
    comments = [r for r in records(jira.content({})) if r.source_type == COMMENT]

    assert comments
    for comment in comments:
        assert comment.container is not None
        assert comment.container.source_type == ISSUE
        assert comment.source_id.startswith(comment.container.source_id + ":")


def test_comment_ids_are_qualified_by_issue(jira: JiraConnector) -> None:
    ids = [r.source_id for r in records(jira.content({})) if r.source_type == COMMENT]

    assert len(ids) == len(set(ids))


def test_an_issue_with_no_comments_is_fine(jira: JiraConnector) -> None:
    ids = {r.source_id for r in records(jira.content({})) if r.source_type == ISSUE}

    assert "ACME-2" in ids


# ---------------------------------------------------------------------------
# ACLs, from project roles.
# ---------------------------------------------------------------------------


def test_a_project_grants_to_the_actors_in_its_roles(jira: JiraConnector) -> None:
    grants = [r for r in records(jira.acls({})) if r.target.source_id == "ACME"]

    assert {g.principal_source_id for g in grants} == {"g-dev"}


def test_both_user_and_group_actors_are_understood(jira: JiraConnector) -> None:
    """PUB names a group in one role and a person directly in another."""
    grants = [r for r in records(jira.acls({})) if r.target.source_id == "PUB"]

    assert {g.principal_source_id for g in grants} == {"g-dev", "u-carol"}


def test_a_principal_holding_two_roles_is_granted_once(jira: JiraConnector) -> None:
    """g-dev is in both PUB roles."""
    grants = [r.principal_source_id for r in records(jira.acls({})) if r.target.source_id == "PUB"]

    assert grants.count("g-dev") == 1


def test_acls_target_projects_not_issues(jira: JiraConnector) -> None:
    """One grant per project. Containment carries it to issues and comments."""
    assert all(r.target.source_type == PROJECT for r in records(jira.acls({})))


# ---------------------------------------------------------------------------
# Transport.
# ---------------------------------------------------------------------------


def test_fixture_filenames_encode_the_path() -> None:
    assert FixtureTransport.filename("project/search") == "project.search.json"
    assert FixtureTransport.filename("issue/ACME-1/comment") == "issue.ACME-1.comment.json"


def test_group_membership_fixtures_are_per_group() -> None:
    """group/member identifies its group by query parameter, so the filename
    has to carry it or every group would share one fixture."""
    assert (
        FixtureTransport.filename("group/member", {"groupId": "g-dev"}) == "group.member.g-dev.json"
    )


def test_the_fixture_transport_pages_the_dataset() -> None:
    """Fixtures hold the whole dataset and the transport slices it, so the same
    files exercise every page size."""
    transport = FixtureTransport(FIXTURES)

    first = transport.get("project/search", {"startAt": 0, "maxResults": 1})
    second = transport.get("project/search", {"startAt": 1, "maxResults": 1})

    assert [p["key"] for p in first["values"]] == ["ACME"]
    assert first["isLast"] is False
    assert [p["key"] for p in second["values"]] == ["PUB"]
    assert second["isLast"] is True


def test_a_missing_fixture_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(PermanentSourceError, match="no recorded response"):
        FixtureTransport(tmp_path).get("project/search")


def test_unpaginated_responses_pass_through() -> None:
    roles = FixtureTransport(FIXTURES).get("project/ACME/role")

    assert "Developers" in roles


def _http(handler: Any) -> HttpTransport:
    return HttpTransport(
        "https://example.atlassian.net",
        "bot@example.com",
        "token",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_http_transport_uses_basic_auth_against_the_api_root() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"values": []})

    _http(handler).get("project/search", {"startAt": 0})

    assert seen["auth"].startswith("Basic ")
    assert "/rest/api/3/project/search" in seen["url"]
    assert "startAt=0" in seen["url"]


def test_http_429_becomes_a_rate_limit() -> None:
    transport = _http(lambda request: httpx.Response(429, headers={"Retry-After": "30"}, json={}))

    with pytest.raises(RateLimitedError) as caught:
        transport.get("project/search")

    assert caught.value.retry_after == 30.0


def test_a_rate_limit_without_a_usable_hint() -> None:
    transport = _http(
        lambda request: httpx.Response(429, headers={"Retry-After": "later"}, json={})
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.get("project/search")

    assert caught.value.retry_after is None


def test_server_errors_are_transient() -> None:
    with pytest.raises(TransientSourceError):
        _http(lambda request: httpx.Response(502, json={})).get("project/search")


@pytest.mark.parametrize("status", [401, 403, 404])
def test_auth_and_missing_resource_errors_are_permanent(status: int) -> None:
    """Retrying a permissions problem only delays the alert."""
    with pytest.raises(PermanentSourceError):
        _http(lambda request: httpx.Response(status, json={})).get("project/search")


def test_network_failures_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(TransientSourceError):
        _http(handler).get("project/search")


# ---------------------------------------------------------------------------
# The write verbs (P1-SYNC-5). Still no live call: httpx.MockTransport answers.
# ---------------------------------------------------------------------------


def test_a_post_carries_the_body_and_returns_the_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert json.loads(request.content) == {"body": "hello"}
        return httpx.Response(201, json={"id": "10100"})

    assert _http(handler).post("issue/ACME-1/comment", {"body": "hello"}) == {"id": "10100"}


def test_a_delete_sends_no_body() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.method)
        return httpx.Response(204)

    _http(handler).delete("issue/ACME-1/comment/10100")

    assert seen == ["DELETE"]


def test_an_empty_response_is_not_a_parse_error() -> None:
    """Jira answers 204 on a delete and empty on a transition. Trying to decode
    that as JSON would turn a success into an exception."""
    transport = _http(lambda request: httpx.Response(204))

    assert transport.post("issue/ACME-1/transitions", {"transition": {"id": "21"}}) is None


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_write_that_will_not_start_working_is_permanent(status: int) -> None:
    """One error taxonomy for every verb. A write path with its own idea of
    which failures are retryable retries a 403 forever, and for an approved
    action that means a human's decision quietly evaporating."""
    with pytest.raises(PermanentSourceError):
        _http(lambda request: httpx.Response(status, json={})).post("issue/ACME-1/comment", {})


def test_a_write_that_might_work_later_is_transient() -> None:
    with pytest.raises(TransientSourceError):
        _http(lambda request: httpx.Response(503, json={})).post("issue/ACME-1/comment", {})


def test_a_rate_limited_write_carries_the_hint() -> None:
    transport = _http(lambda request: httpx.Response(429, headers={"Retry-After": "30"}, json={}))

    with pytest.raises(RateLimitedError) as caught:
        transport.post("issue/ACME-1/comment", {})

    assert caught.value.retry_after == 30.0


def test_a_dropped_write_connection_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with pytest.raises(TransientSourceError):
        _http(handler).delete("issue/ACME-1/comment/1")


def test_the_error_names_the_verb() -> None:
    """A log line saying 'ACME-1: HTTP 403' does not say whether the read or
    the write was refused."""
    with pytest.raises(PermanentSourceError, match="POST"):
        _http(lambda request: httpx.Response(403, json={})).post("issue/ACME-1/comment", {})


def test_the_fixture_refuses_a_write_it_does_not_model(tmp_path: Path) -> None:
    transport = FixtureTransport(tmp_path)

    with pytest.raises(PermanentSourceError, match="no recorded write"):
        transport.post("issue/ACME-1/attachments", {})


def test_a_transitioned_issue_reads_back_with_its_new_status() -> None:
    """The fixture stays coherent with its own writes, so a second capture sees
    what the first write did rather than the file on disk."""
    transport = FixtureTransport(FIXTURES)
    transport.post("issue/ACME-1/transitions", {"transition": {"id": "21", "name": "Done"}})

    body = transport.get("issue/ACME-1")

    assert body["fields"]["status"]["name"] == "Done"


def test_the_fixture_has_no_status_for_an_unknown_issue(tmp_path: Path) -> None:
    """Every transition is on offer when nothing is known, which is the safe
    direction for a fixture: it cannot hide a missing one."""
    transport = FixtureTransport(tmp_path)

    offered = transport.get("issue/NOSUCH-1/transitions")

    assert len(offered["transitions"]) == len(FixtureTransport.STATUSES)
