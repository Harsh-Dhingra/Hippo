"""P3-CON-1: GitHub, and whether SDK v1 actually generalises.

The third connector exists to test the contract, not to add a source. So the
tests worth the most are the ones covering what GitHub does that neither Slack
nor Jira does: a cursor that lives in a response header rather than in a
payload, two different rate limits arriving on the same status code, visibility
expressed as public-or-private rather than as a membership list, and a
write-back whose inverse is a state to restore rather than a thing to delete.

Every one of those is a place the SDK could have been too narrow. If it is, it
shows here rather than in a contributor's repository six months from now.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from sync.connectors.github import FixtureTransport, GitHubConnector
from sync.connectors.github.actions import CLOSE_ACTION, COMMENT_ACTION, ClosePayload
from sync.connectors.github.connector import ISSUE, ORG_GROUP, REPO
from sync.connectors.github.transport import HttpTransport, Response
from sync.connectors.harness import WritebackCase, assert_conforms, check_connector
from sync.connectors.sdk import (
    DONE,
    PermanentSourceError,
    RateLimitedError,
    SourceRef,
    TransientSourceError,
    WritebackRequest,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "github"
ORG = "acme"

BILLING_ISSUE = SourceRef(source_type=ISSUE, source_id="acme/billing#7")


def build(page_size: int = 100) -> GitHubConnector:
    return GitHubConnector(FixtureTransport(FIXTURES, page_size=page_size), ORG)


def drain(stream: Any, cursor: Any = None) -> list[Any]:
    records: list[Any] = []
    current = dict(cursor or {})
    while True:
        page = next(iter(stream(current)))
        records.extend(page.records)
        current = page.cursor
        if not page.has_more:
            return records


# ---------------------------------------------------------------------------
# Conformance, which is the point of writing a third one.
# ---------------------------------------------------------------------------


def test_the_connector_conforms() -> None:
    connector = build()
    assert_conforms(connector, writeback=connector)


@pytest.mark.parametrize("page_size", [1, 2, 3, 100])
def test_it_conforms_at_every_page_size(page_size: int) -> None:
    """Page boundaries are where cursor bugs live, and GitHub's cursor is a URL
    the server built rather than an offset this connector controls."""
    connector = build(page_size)
    assert check_connector(connector, writeback=connector) == (), f"page_size={page_size}"


def test_the_writeback_round_trips() -> None:
    """Both shapes: a comment undone by deleting what was created, and a close
    undone by restoring the state that was read beforehand."""
    transport = FixtureTransport(FIXTURES)
    connector = GitHubConnector(transport, ORG)

    assert_conforms(
        connector,
        writeback=connector,
        writeback_cases=[
            WritebackCase(
                request=WritebackRequest(
                    action_type=COMMENT_ACTION,
                    target=BILLING_ISSUE,
                    payload={"body": "Legal signed off on the cap this morning."},
                ),
                observe=lambda: _comment_count(transport),
            ),
            WritebackCase(
                request=WritebackRequest(
                    action_type=CLOSE_ACTION,
                    target=BILLING_ISSUE,
                    payload={"reason": "completed"},
                ),
                observe=lambda: _issue_state(connector),
            ),
        ],
    )


def _comment_count(transport: FixtureTransport) -> int:
    return sum(len(comments) for comments in transport.comments.values())


def test_rolling_back_a_comment_without_its_id_refuses() -> None:
    """Deleting a guessed comment is worse than refusing. The id arrives in the
    inverse only because the executor folds the receipt in after a successful
    create; nothing captured beforehand could know it."""
    connector = build()
    request = WritebackRequest(
        action_type=COMMENT_ACTION, target=BILLING_ISSUE, payload={"body": "x"}
    )
    inverse = connector.capture_inverse(request)

    with pytest.raises(Exception, match="nothing safe to delete"):
        connector.rollback(request, inverse)


def _issue_state(connector: GitHubConnector) -> str:
    request = WritebackRequest(action_type=CLOSE_ACTION, target=BILLING_ISSUE)
    return str(connector.capture_inverse(request)["state"])


# ---------------------------------------------------------------------------
# The cursor lives in a header.
# ---------------------------------------------------------------------------


def test_a_link_header_becomes_the_cursor() -> None:
    """The runtime treats a cursor as opaque jsonb, which is the only reason a
    server-built URL can be stored as one. A specified cursor shape would have
    forced a translation layer here that guessed at page boundaries."""
    connector = build(page_size=1)

    first = next(iter(connector.content({})))

    assert first.has_more is True
    assert "page=2" in str(first.cursor.get("next", ""))


def test_paging_through_content_yields_every_issue() -> None:
    """The property that matters: everything arrives, whatever the page size."""
    at_once = {r.source_id for r in drain(build(100).content)}
    one_at_a_time = {r.source_id for r in drain(build(1).content)}

    assert at_once == one_at_a_time
    assert "acme/billing#7" in at_once
    assert "acme/web#1" in at_once


def test_the_cursor_carries_the_repository_list() -> None:
    """A repository created mid-sync would otherwise shift every later index
    and silently skip whatever moved past the cursor."""
    connector = build(page_size=1)

    first = next(iter(connector.content({})))

    assert first.cursor["repos"] == ["acme/web", "acme/billing"]


def test_content_ends_with_a_terminal_cursor() -> None:
    records = drain(build().content)

    assert records
    last = None
    cursor: dict[str, Any] = {}
    while True:
        last = next(iter(build().content(cursor)))
        cursor = last.cursor
        if not last.has_more:
            break
    assert last.cursor.get(DONE) is True


# ---------------------------------------------------------------------------
# Visibility is public-or-private, not a membership list.
# ---------------------------------------------------------------------------


def test_a_public_repository_grants_to_the_org_group() -> None:
    """One row that stays correct when somebody joins, rather than a row per
    member per repository that is wrong the moment they do."""
    grants = drain(build().acls)

    web = [g for g in grants if g.target.source_id == "acme/web"]

    assert [g.principal_source_id for g in web] == [ORG_GROUP]


def test_a_private_repository_grants_to_collaborators_and_teams() -> None:
    grants = drain(build().acls)

    billing = {g.principal_source_id for g in grants if g.target.source_id == "acme/billing"}

    assert billing == {"alice", "team:billing-team"}


def test_every_acl_target_is_a_repository() -> None:
    """Access is granted on repositories. Granting per issue would be correct
    and unusably slow on a real organisation."""
    grants = drain(build().acls)

    assert {g.target.source_type for g in grants} == {REPO}


def test_content_declares_its_repository_as_its_container() -> None:
    """Which is what lets one ACL row per repository cover its issues."""
    records = drain(build().content)

    containers = {r.container.source_type for r in records if r.container}

    assert containers == {REPO}


def test_a_comment_is_contained_by_the_repository_not_the_issue() -> None:
    """Truer to the conversation would be the issue, and it would leave the
    comment ungranted: the permission filter reads containers, and access is
    granted on repositories."""
    records = drain(build().content)

    comments = [r for r in records if r.source_type == "github.comment"]

    assert comments
    assert all(r.container and r.container.source_type == REPO for r in comments)


# ---------------------------------------------------------------------------
# Identities.
# ---------------------------------------------------------------------------


def test_the_org_group_is_emitted_before_anything_grants_to_it() -> None:
    """A sync producing repositories before the group they grant to would leave
    those grants unprojectable until the next pass."""
    records = drain(build().identities)

    assert records[0].source_id == ORG_GROUP
    assert records[0].kind == "group"


def test_a_person_carries_every_team_they_are_in() -> None:
    """GitHub only offers membership the other way round, so the connector
    inverts it — and getting that wrong would silently narrow what people see."""
    people = {r.source_id: r for r in drain(build().identities) if r.kind == "user"}

    assert set(people["alice"].member_of) == {ORG_GROUP, "team:billing-team"}
    assert set(people["carla"].member_of) == {ORG_GROUP, "team:web-team"}


def test_a_member_without_a_public_email_is_synced_anyway() -> None:
    """GitHub hides an email unless the account published it. Without one,
    identity resolution cannot link this person to their Slack or Jira account,
    and the honest outcome is that it does not try rather than guessing from a
    username."""
    people = {r.source_id: r for r in drain(build().identities) if r.kind == "user"}

    assert people["carla"].email is None
    assert people["alice"].email == "alice@acme.example"


def test_payloads_are_verbatim() -> None:
    """A field this connector has never heard of is stored, not dropped."""
    records = drain(build().content)

    issue = next(r for r in records if r.source_id == "acme/billing#7")

    assert issue.payload["title"] == "Acme renewal blocked on the liability cap"
    assert issue.payload["node_id"] == "I_bill_7"


# ---------------------------------------------------------------------------
# Write-back: an inverse that is a state, not a thing to delete.
# ---------------------------------------------------------------------------


def test_closing_captures_the_state_to_restore() -> None:
    """The first write-back whose inverse is not "delete what we made". An
    issue closed and then reopened by somebody else must not be closed again by
    a rollback, which is only possible because the state was read beforehand."""
    connector = build()
    request = WritebackRequest(action_type=CLOSE_ACTION, target=BILLING_ISSUE)

    inverse = connector.capture_inverse(request)

    assert inverse["kind"] == "state"
    assert inverse["state"] == "open"


def test_closing_and_rolling_back_restores_the_state() -> None:
    connector = build()
    request = WritebackRequest(
        action_type=CLOSE_ACTION, target=BILLING_ISSUE, payload={"reason": "completed"}
    )
    inverse = connector.capture_inverse(request)

    connector.execute(request)
    assert _issue_state(connector) == "closed"

    connector.rollback(request, inverse)
    assert _issue_state(connector) == "open"


def test_commenting_returns_the_id_needed_to_undo_it() -> None:
    connector = build()
    request = WritebackRequest(
        action_type=COMMENT_ACTION, target=BILLING_ISSUE, payload={"body": "noted"}
    )
    connector.capture_inverse(request)

    receipt = connector.execute(request)

    assert receipt.external_id


def test_an_unreadable_target_fails_rather_than_executing() -> None:
    """CLAUDE.md rule 3. No inverse capture means the action fails; it does not
    mean the action executes without a rollback path."""
    connector = build()
    missing = SourceRef(source_type=ISSUE, source_id="acme/web#999")

    with pytest.raises(Exception, match="cannot read"):
        connector.capture_inverse(WritebackRequest(action_type=CLOSE_ACTION, target=missing))


def test_a_target_that_is_not_an_issue_is_refused() -> None:
    connector = build()
    repo = SourceRef(source_type=REPO, source_id="acme/web")

    with pytest.raises(Exception, match="needs an issue target"):
        connector.capture_inverse(WritebackRequest(action_type=CLOSE_ACTION, target=repo))


def test_an_unknown_action_is_refused() -> None:
    connector = build()

    with pytest.raises(Exception, match="cannot perform"):
        connector.capture_inverse(
            WritebackRequest(action_type="github.delete", target=BILLING_ISSUE)
        )


def test_the_close_reason_is_constrained_to_githubs_vocabulary() -> None:
    """A free string would be rejected by the API after a person had already
    approved the action."""
    with pytest.raises(Exception, match="Input should be"):
        ClosePayload(reason="because-i-said-so")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Two rate limits on one status code.
# ---------------------------------------------------------------------------


def transport_returning(status: int, headers: dict[str, str]) -> HttpTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers, json={})

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )
    return transport


def test_a_throttling_403_is_a_rate_limit() -> None:
    """GitHub's abuse limiter answers 403 with Retry-After. Dead-lettering it
    would drop a stream that was only being asked to wait."""
    transport = transport_returning(403, {"Retry-After": "30"})

    with pytest.raises(RateLimitedError) as caught:
        transport.get("/orgs/acme/members")

    assert caught.value.retry_after == 30.0


def test_a_forbidding_403_is_permanent() -> None:
    """The same status code meaning the opposite thing. Retrying this forever
    burns the budget and never succeeds."""
    transport = transport_returning(403, {})

    with pytest.raises(PermanentSourceError):
        transport.get("/orgs/acme/members")


def test_an_exhausted_primary_quota_is_a_rate_limit() -> None:
    transport = transport_returning(403, {"X-RateLimit-Remaining": "0"})

    with pytest.raises(RateLimitedError):
        transport.get("/orgs/acme/members")


def test_the_reset_header_is_read_against_githubs_clock() -> None:
    """X-RateLimit-Reset is an absolute epoch time, not a delay. Reading it as
    a delay means sleeping until 2026; reading it against our own clock means
    a machine minutes out of sync sleeps for minutes too long or not at all."""
    transport = transport_returning(
        403,
        {
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": "1800000060",
            "Date": "Fri, 15 Jan 2027 08:00:00 GMT",
        },
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.get("/orgs/acme/members")

    # 1800000060 is 2027-01-15T08:01:00Z, sixty seconds after the Date header.
    assert caught.value.retry_after == pytest.approx(60.0, abs=1.0)


def test_a_server_error_is_transient() -> None:
    transport = transport_returning(503, {})

    with pytest.raises(TransientSourceError):
        transport.get("/orgs/acme/members")


def test_a_missing_resource_is_permanent() -> None:
    """GitHub hides what you cannot see behind a 404, so this is also how a
    private repository answers."""
    transport = transport_returning(404, {})

    with pytest.raises(PermanentSourceError):
        transport.get("/repos/acme/secret/issues")


def test_a_link_header_is_parsed_out_of_the_whole_set() -> None:
    """GitHub sends every rel it has, in an order it does not promise."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={
                "Link": (
                    '<https://api.github.com/x?page=5>; rel="last", '
                    '<https://api.github.com/x?page=2>; rel="next"'
                )
            },
            json=[],
        )

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    response = transport.get("/x")

    assert response.next_url == "https://api.github.com/x?page=2"


def test_a_response_with_no_next_link_ends_the_stream() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Link": '<https://x>; rel="prev"'}, json=[])

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    assert transport.get("/x").next_url is None


def test_a_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow")

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    with pytest.raises(TransientSourceError):
        transport.get("/x")


# ---------------------------------------------------------------------------
# Registration.
# ---------------------------------------------------------------------------


def test_github_is_registered_with_its_actions() -> None:
    from sync.connectors import registry

    plugin = registry.plugin_for("github")

    assert plugin.capabilities.action_types == {COMMENT_ACTION, CLOSE_ACTION}
    assert plugin.requires_config == ("org",)


def test_the_agent_can_propose_a_github_action() -> None:
    """The SDK v1 claim, checked on a connector added after it: an action
    declared by a connector reaches the agent's vocabulary without any edit to
    the agent."""
    from agent.actions import actions

    assert COMMENT_ACTION in actions()
    assert actions()[CLOSE_ACTION].targets == frozenset({ISSUE})


def test_a_connector_row_without_an_org_is_refused() -> None:
    """There is no sensible default, and guessing would sync somebody else's
    public repositories."""
    from sync.connectors import registry

    with pytest.raises(registry.MissingConfigError, match="org"):
        registry.plugin_for("github").connector({}, "token")


def test_the_connector_needs_an_organisation() -> None:
    with pytest.raises(PermanentSourceError, match="organisation"):
        GitHubConnector(FixtureTransport(FIXTURES), "")


def test_the_transport_response_carries_body_and_cursor() -> None:
    response = Response({"a": 1}, "https://next")

    assert response.body == {"a": 1}
    assert response.next_url == "https://next"


# ---------------------------------------------------------------------------
# Resuming, and the paths a real sync takes that a happy path does not.
# ---------------------------------------------------------------------------


def test_resuming_from_a_terminal_cursor_yields_one_empty_page() -> None:
    """What makes the resume contract exact for a listing stream: the runtime
    stores a terminal cursor and hands it straight back on the next tick."""
    for stream in ("identities", "content", "acls"):
        pages = list(getattr(build(), stream)({DONE: True}))
        assert len(pages) == 1, stream
        assert pages[0].records == (), stream
        assert pages[0].has_more is False, stream


def test_a_cursor_past_the_end_of_the_repository_list_is_finished() -> None:
    """A repository deleted between two syncs leaves a stored index pointing
    past the end. Finishing is right; raising would wedge the stream."""
    page = next(iter(build().content({"repos": ["acme/web"], "index": 5})))

    assert page.records == ()
    assert page.cursor == {DONE: True}


def test_the_live_transport_posts_and_patches() -> None:
    """The write verbs, which the fixture transport cannot exercise."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"id": 1})

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    transport.post("/repos/acme/web/issues/1/comments", {"body": "x"})
    transport.patch("/repos/acme/web/issues/1", {"state": "closed"})
    transport.delete("/repos/acme/web/issues/comments/1")

    assert [method for method, _ in seen] == ["POST", "PATCH", "DELETE"]


def test_a_malformed_retry_after_is_ignored_rather_than_guessed() -> None:
    transport = transport_returning(403, {"Retry-After": "soon"})

    with pytest.raises(RateLimitedError) as caught:
        transport.get("/x")

    assert caught.value.retry_after is None


def test_a_reset_header_without_a_date_is_not_used() -> None:
    """X-RateLimit-Reset is an absolute time on GitHub's clock. Without knowing
    that clock, subtracting our own would sleep for however far apart they are."""
    transport = transport_returning(
        403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1800000060"}
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.get("/x")

    assert caught.value.retry_after is None


def test_unparseable_rate_limit_headers_do_not_crash() -> None:
    transport = transport_returning(
        403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "never", "Date": "yesterday"}
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.get("/x")

    assert caught.value.retry_after is None


def test_a_429_is_a_rate_limit_whatever_the_headers_say() -> None:
    transport = transport_returning(429, {})

    with pytest.raises(RateLimitedError):
        transport.get("/x")


def test_a_connection_error_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    transport = HttpTransport("token")
    transport._client = httpx.Client(
        transport=httpx.MockTransport(handler), base_url="https://api.github.com"
    )

    with pytest.raises(TransientSourceError):
        transport.get("/x")


def test_the_fixture_transport_answers_a_missing_file_the_way_github_does() -> None:
    """404, because GitHub hides what you cannot see rather than admitting it.
    An empty list would teach the connector that private means absent."""
    transport = FixtureTransport(FIXTURES)

    with pytest.raises(PermanentSourceError, match="404"):
        transport.get("/repos/acme/nothing/issues")


def test_rolling_back_an_unknown_inverse_is_refused() -> None:
    connector = build()
    request = WritebackRequest(action_type=COMMENT_ACTION, target=BILLING_ISSUE)

    with pytest.raises(PermanentSourceError, match="cannot roll back"):
        connector.rollback(request, {"kind": "something-else"})


def test_executing_an_unknown_action_is_refused() -> None:
    connector = build()

    with pytest.raises(PermanentSourceError, match="cannot perform"):
        connector.execute(WritebackRequest(action_type="github.merge", target=BILLING_ISSUE))


def test_an_unparseable_issue_id_is_refused() -> None:
    connector = build()
    nonsense = SourceRef(source_type=ISSUE, source_id="not-an-issue")

    with pytest.raises(Exception, match="unrecognised issue id"):
        connector.capture_inverse(WritebackRequest(action_type=CLOSE_ACTION, target=nonsense))


def test_a_writeback_with_no_target_is_refused() -> None:
    connector = build()

    with pytest.raises(Exception, match="needs an issue target"):
        connector.capture_inverse(WritebackRequest(action_type=CLOSE_ACTION))
