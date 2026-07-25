"""The Slack connector, against recorded responses."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from sync.connectors.harness import assert_conforms, check_connector
from sync.connectors.sdk import (
    PermanentSourceError,
    RateLimitedError,
    TransientSourceError,
)
from sync.connectors.slack import FixtureTransport, HttpTransport, SlackConnector
from sync.connectors.slack.connector import CHANNEL, MESSAGE, WORKSPACE_GROUP

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slack"


@pytest.fixture
def slack() -> SlackConnector:
    return SlackConnector(FixtureTransport(FIXTURES))


def records(pages: Any) -> list[Any]:
    return [record for page in pages for record in page.records]


# ---------------------------------------------------------------------------
# Conformance.
# ---------------------------------------------------------------------------


def test_the_slack_connector_conforms(slack: SlackConnector) -> None:
    """The same harness the mock connector passes, on a real connector."""
    assert_conforms(slack)


def test_conformance_holds_when_paging_finely() -> None:
    """Slack's own cursors drive paging here, so this mostly re-checks that
    multi-page fixtures resume correctly."""
    assert check_connector(SlackConnector(FixtureTransport(FIXTURES), limit=1)) == ()


# ---------------------------------------------------------------------------
# Identities.
# ---------------------------------------------------------------------------


def test_users_become_identities_with_emails(slack: SlackConnector) -> None:
    identities = records(slack.identities({}))
    users = {r.source_id: r for r in identities if r.kind == "user"}

    assert set(users) == {"U-ALICE", "U-BOB", "U-CAROL"}
    assert users["U-ALICE"].email == "alice@example.com"
    assert users["U-ALICE"].display_name == "Alice Okafor"


def test_deleted_and_bot_users_are_skipped(slack: SlackConnector) -> None:
    """A departed employee must not keep a principal that grants keep pointing at."""
    ids = {r.source_id for r in records(slack.identities({}))}

    assert "U-GONE" not in ids
    assert "B-HIPPO" not in ids


def test_the_workspace_group_is_emitted_once(slack: SlackConnector) -> None:
    identities = records(slack.identities({}))
    groups = [r for r in identities if r.kind == "group"]

    assert [g.source_id for g in groups] == [WORKSPACE_GROUP]


def test_every_user_belongs_to_the_workspace_group(slack: SlackConnector) -> None:
    """This is what makes a public channel readable without enumerating members."""
    users = [r for r in records(slack.identities({})) if r.kind == "user"]

    assert all(WORKSPACE_GROUP in user.member_of for user in users)


def test_identity_payloads_keep_unmodelled_fields(slack: SlackConnector) -> None:
    carol = next(r for r in records(slack.identities({})) if r.source_id == "U-CAROL")

    assert carol.payload["an_unmodelled_field"] == "stored verbatim anyway"


def test_the_workspace_group_is_not_repeated_on_resume(slack: SlackConnector) -> None:
    """Resuming mid-stream must yield the suffix, not re-emit the header."""
    first, *_ = list(slack.identities({}))
    resumed = records(slack.identities(first.cursor))

    assert all(r.kind == "user" for r in resumed)


# ---------------------------------------------------------------------------
# Content.
# ---------------------------------------------------------------------------


def test_channels_and_messages_are_both_content(slack: SlackConnector) -> None:
    content = records(slack.content({}))
    by_type: dict[str, set[str]] = {}
    for record in content:
        by_type.setdefault(record.source_type, set()).add(record.source_id)

    assert by_type[CHANNEL] == {"C-GENERAL", "C-DEALS"}
    assert "C-GENERAL:1750000000.000100" in by_type[MESSAGE]


def test_messages_declare_their_channel_as_container(slack: SlackConnector) -> None:
    """The whole ACL inheritance story rests on this."""
    messages = [r for r in records(slack.content({})) if r.source_type == MESSAGE]

    assert messages, "expected messages"
    for message in messages:
        assert message.container is not None
        assert message.container.source_type == CHANNEL
        assert message.source_id.startswith(message.container.source_id + ":")


def test_message_ids_are_qualified_by_channel(slack: SlackConnector) -> None:
    """A Slack ts is only unique within a channel; source_id must be unique
    within the connector or two messages would upsert onto one row."""
    ids = [r.source_id for r in records(slack.content({})) if r.source_type == MESSAGE]

    assert len(ids) == len(set(ids))
    assert all(":" in source_id for source_id in ids)


def test_thread_replies_are_synced(slack: SlackConnector) -> None:
    """conversations.history returns thread parents only, so without replies
    most of the actual conversation never arrives."""
    texts = {r.payload.get("text") for r in records(slack.content({})) if r.source_type == MESSAGE}

    assert "legal review is the blocker, not engineering" in texts
    assert "agreed, engineering is done" in texts


def test_a_thread_parent_is_not_duplicated_by_its_replies(slack: SlackConnector) -> None:
    ids = [r.source_id for r in records(slack.content({})) if r.source_type == MESSAGE]

    assert ids.count("C-GENERAL:1750000000.000100") == 1


def test_private_channel_messages_are_synced_like_any_other(slack: SlackConnector) -> None:
    """Sync fetches everything; visibility is decided later, by the filter."""
    ids = {r.source_id for r in records(slack.content({})) if r.source_type == MESSAGE}

    assert "C-DEALS:1750000200.000100" in ids


# ---------------------------------------------------------------------------
# ACLs. The reason this connector exists.
# ---------------------------------------------------------------------------


def test_a_public_channel_grants_to_the_whole_workspace(slack: SlackConnector) -> None:
    """Not to its members: a public channel is readable by anyone in the
    workspace, joined or not."""
    grants = [r for r in records(slack.acls({})) if r.target.source_id == "C-GENERAL"]

    assert [g.principal_source_id for g in grants] == [WORKSPACE_GROUP]


def test_a_private_channel_grants_only_to_its_members(slack: SlackConnector) -> None:
    """The demo in ARCHITECTURE section 12 point 2, at the connector level."""
    grants = [r for r in records(slack.acls({})) if r.target.source_id == "C-DEALS"]

    assert {g.principal_source_id for g in grants} == {"U-ALICE", "U-BOB"}
    assert WORKSPACE_GROUP not in {g.principal_source_id for g in grants}
    assert "U-CAROL" not in {g.principal_source_id for g in grants}


def test_acls_target_channels_not_messages(slack: SlackConnector) -> None:
    """One grant per channel, not one per message. Inheritance does the rest."""
    assert all(r.target.source_type == CHANNEL for r in records(slack.acls({})))


def test_members_are_not_fetched_for_public_channels(slack: SlackConnector) -> None:
    """Calling conversations.members on every public channel would be a large
    and pointless amount of API traffic."""
    transport = FixtureTransport(FIXTURES)
    connector = SlackConnector(transport)

    records(connector.acls({}))

    members_calls = [
        params for method, params in transport.calls if method == "conversations.members"
    ]
    assert [call["channel"] for call in members_calls] == ["C-DEALS"]


# ---------------------------------------------------------------------------
# Transport.
# ---------------------------------------------------------------------------


def test_a_missing_fixture_fails_loudly(tmp_path: Path) -> None:
    transport = FixtureTransport(tmp_path)

    with pytest.raises(PermanentSourceError, match="no recorded response"):
        transport.call("users.list", {})


def test_a_missing_fixture_page_fails_loudly() -> None:
    transport = FixtureTransport(FIXTURES)

    with pytest.raises(PermanentSourceError, match="no page for cursor"):
        transport.call("users.list", {"cursor": "nonexistent"})


def test_fixture_filenames_encode_the_call() -> None:
    assert FixtureTransport.filename("users.list", {}) == "users.list.json"
    assert (
        FixtureTransport.filename("conversations.history", {"channel": "C-1"})
        == "conversations.history.C-1.json"
    )
    assert (
        FixtureTransport.filename("conversations.replies", {"channel": "C-1", "ts": "9.9"})
        == "conversations.replies.C-1.9.9.json"
    )


def _http(handler: Any) -> HttpTransport:
    return HttpTransport("xoxb-test", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_http_transport_returns_the_body() -> None:
    transport = _http(lambda request: httpx.Response(200, json={"ok": True, "members": []}))

    assert transport.call("users.list", {"cursor": ""}) == {"ok": True, "members": []}


def test_http_transport_sends_the_token_and_drops_empty_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"ok": True})

    _http(handler).call("users.list", {"cursor": "", "limit": 200})

    assert seen["auth"] == "Bearer xoxb-test"
    assert "cursor" not in seen["url"], "an empty cursor must not be sent"
    assert "limit=200" in seen["url"]


def test_http_429_becomes_a_rate_limit_with_the_sources_hint() -> None:
    transport = _http(
        lambda request: httpx.Response(429, headers={"Retry-After": "12"}, json={"ok": False})
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.call("users.list", {})

    assert caught.value.retry_after == 12.0


def test_a_rate_limit_without_a_hint_is_still_a_rate_limit() -> None:
    transport = _http(lambda request: httpx.Response(429, json={"ok": False}))

    with pytest.raises(RateLimitedError) as caught:
        transport.call("users.list", {})

    assert caught.value.retry_after is None


def test_an_unparseable_retry_after_is_ignored() -> None:
    transport = _http(
        lambda request: httpx.Response(429, headers={"Retry-After": "soon"}, json={"ok": False})
    )

    with pytest.raises(RateLimitedError) as caught:
        transport.call("users.list", {})

    assert caught.value.retry_after is None


def test_slack_ratelimited_error_body_is_a_rate_limit() -> None:
    transport = _http(
        lambda request: httpx.Response(200, json={"ok": False, "error": "ratelimited"})
    )

    with pytest.raises(RateLimitedError):
        transport.call("users.list", {})


def test_server_errors_are_transient() -> None:
    transport = _http(lambda request: httpx.Response(503, json={"ok": False}))

    with pytest.raises(TransientSourceError):
        transport.call("users.list", {})


def test_client_errors_are_permanent() -> None:
    transport = _http(lambda request: httpx.Response(404, json={"ok": False}))

    with pytest.raises(PermanentSourceError):
        transport.call("users.list", {})


@pytest.mark.parametrize("error", ["invalid_auth", "missing_scope", "channel_not_found"])
def test_fatal_slack_errors_are_permanent(error: str) -> None:
    transport = _http(lambda request: httpx.Response(200, json={"ok": False, "error": error}))

    with pytest.raises(PermanentSourceError, match=error):
        transport.call("users.list", {})


def test_unrecognised_slack_errors_are_treated_as_transient() -> None:
    """The safe direction: a retry costs time, a wrongly permanent failure
    costs the record."""
    transport = _http(
        lambda request: httpx.Response(200, json={"ok": False, "error": "something_new"})
    )

    with pytest.raises(TransientSourceError):
        transport.call("users.list", {})


def test_network_failures_are_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TransientSourceError):
        _http(handler).call("users.list", {})
