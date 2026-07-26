"""P3-SRF-1: Hippo in Slack.

Two things here are dangerous in ways no other surface is, and they are what
most of this file is about.

**An unsigned webhook is a hole, not a surface.** These endpoints will approve
a write into Jira. Without signature verification, anyone who finds the URL can
do it. So the verifier is tested against a forged signature, a replayed one, a
missing one, and an install with no secret at all — and there is no development
mode that skips it.

**An answer posted into a channel is a leak dressed as a feature.** Hippo
filters an answer to what the asking person may read. Putting it in #general
shows it to everybody in #general. Every response is ephemeral and
`response_type` is not something a caller can set.

Approving *is* allowed here, unlike from the MCP surface, and the difference is
who clicks: Slack names the human who pressed the button. The last section is
about checking that it was the right one.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from surfaces.slack.signing import MAX_SKEW_SECONDS, SignatureError, sign, verify

pytestmark = pytest.mark.requires_db

SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
TEAM = "T-ACME"
ALICE_SLACK = "U-ALICE"
BOB_SLACK = "U-BOB"


# ---------------------------------------------------------------------------
# Signing. Everything else rests on this.
# ---------------------------------------------------------------------------


def test_a_genuine_request_verifies() -> None:
    body = b"token=x&text=hello"
    stamp = str(int(time.time()))

    verify(SECRET, sign(SECRET, stamp, body), stamp, body)


def test_a_forged_signature_is_refused() -> None:
    body = b"token=x&text=hello"
    stamp = str(int(time.time()))

    with pytest.raises(SignatureError, match="does not match"):
        verify(SECRET, "v0=deadbeef", stamp, body)


def test_a_signature_for_a_different_body_is_refused() -> None:
    """The attack this exists for: take a signed request, change what it asks
    for, send it on."""
    stamp = str(int(time.time()))
    signature = sign(SECRET, stamp, b"text=what+is+blocking+the+renewal")

    with pytest.raises(SignatureError):
        verify(SECRET, signature, stamp, b"text=approve+everything")


def test_a_signature_from_another_workspace_is_refused() -> None:
    body, stamp = b"text=hello", str(int(time.time()))

    with pytest.raises(SignatureError):
        verify(SECRET, sign("a-different-secret", stamp, body), stamp, body)


def test_an_old_signature_is_refused() -> None:
    """Without a window, a signature captured from a log or a proxy is valid
    forever — and replaying an approval is what this endpoint is worth
    attacking for."""
    body = b"text=hello"
    stamp = str(int(time.time()) - MAX_SKEW_SECONDS - 1)

    with pytest.raises(SignatureError, match="out of date"):
        verify(SECRET, sign(SECRET, stamp, body), stamp, body)


def test_a_future_signature_is_refused() -> None:
    """Both directions. A future timestamp is as much a sign of tampering as an
    old one, and accepting it would make the window one-sided."""
    body = b"text=hello"
    stamp = str(int(time.time()) + MAX_SKEW_SECONDS + 60)

    with pytest.raises(SignatureError, match="out of date"):
        verify(SECRET, sign(SECRET, stamp, body), stamp, body)


def test_a_signature_inside_the_window_is_accepted() -> None:
    body = b"text=hello"
    stamp = str(int(time.time()) - (MAX_SKEW_SECONDS - 30))

    verify(SECRET, sign(SECRET, stamp, body), stamp, body)


@pytest.mark.parametrize(
    ("signature", "stamp"),
    [(None, "123"), ("v0=abc", None), (None, None), ("", "123"), ("v0=abc", "not-a-number")],
)
def test_a_malformed_request_is_refused(signature: str | None, stamp: str | None) -> None:
    with pytest.raises(SignatureError):
        verify(SECRET, signature, stamp, b"text=hello")


def test_without_a_secret_nothing_verifies() -> None:
    """There is no development mode that skips this, because that mode is the
    one that reaches production."""
    body, stamp = b"text=hello", str(int(time.time()))

    with pytest.raises(SignatureError, match="no Slack signing secret"):
        verify("", sign(SECRET, stamp, body), stamp, body)


def test_the_body_is_hashed_as_bytes() -> None:
    """Re-serialising a parsed form gives a different string — a different
    field order, a different encoding of a space — and the mismatch would be
    intermittent rather than total."""
    stamp = str(int(time.time()))
    ordered = b"a=1&b=2"
    reordered = b"b=2&a=1"

    verify(SECRET, sign(SECRET, stamp, ordered), stamp, ordered)
    with pytest.raises(SignatureError):
        verify(SECRET, sign(SECRET, stamp, ordered), stamp, reordered)


# ---------------------------------------------------------------------------
# Over HTTP, against a real Hippo.
# ---------------------------------------------------------------------------


@pytest.fixture
def slack(db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    from tests.slack_world import workspace

    with workspace(db_dsn, monkeypatch, SECRET) as world:
        yield world


def post(client: TestClient, path: str, form: dict[str, str], *, secret: str = SECRET) -> Any:
    body = urlencode(form).encode()
    stamp = str(int(time.time()))
    return client.post(
        path,
        content=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Slack-Request-Timestamp": stamp,
            "X-Slack-Signature": sign(secret, stamp, body),
        },
    )


def ask(client: TestClient, user: str, text: str, **extra: str) -> Any:
    return post(
        client,
        "/api/v1/slack/commands",
        {"team_id": TEAM, "user_id": user, "text": text, **extra},
    )


def test_an_unsigned_command_is_rejected(slack: Any) -> None:
    response = slack.client.post("/api/v1/slack/commands", content=b"team_id=T&user_id=U&text=hi")

    assert response.status_code == 401


def test_a_command_signed_with_the_wrong_secret_is_rejected(slack: Any) -> None:
    response = ask_with_secret(slack.client, "not-the-secret")

    assert response.status_code == 401


def ask_with_secret(client: TestClient, secret: str) -> Any:
    return post(
        client,
        "/api/v1/slack/commands",
        {"team_id": TEAM, "user_id": ALICE_SLACK, "text": "hello"},
        secret=secret,
    )


def test_a_question_is_answered_to_the_person_who_asked(slack: Any) -> None:
    response = ask(slack.client, ALICE_SLACK, "what is blocking the renewal")

    assert response.status_code == 200
    assert response.json()["response_type"] == "ephemeral"


def test_an_answer_is_never_posted_to_the_channel(slack: Any) -> None:
    """The leak that would arrive dressed as a feature. Hippo filters an answer
    to the asker; posting it into #general shows it to everybody there."""
    responses = [
        ask(slack.client, ALICE_SLACK, "what is blocking the renewal"),
        ask(slack.client, ALICE_SLACK, ""),
        ask(slack.client, "U-NOBODY", "anything"),
    ]

    for response in responses:
        assert response.json()["response_type"] == "ephemeral", response.text
        assert "in_channel" not in response.text


def test_an_answer_holds_only_what_that_person_can_see(slack: Any) -> None:
    """Two people, one corpus, the same question."""
    alice = ask(slack.client, ALICE_SLACK, "liability cap").text
    bob = ask(slack.client, BOB_SLACK, "liability cap").text

    assert "alice's private note" in alice
    assert "alice's private note" not in bob
    assert "bob's private note" in bob
    assert "bob's private note" not in alice


def test_an_unlinked_slack_user_is_told_so(slack: Any) -> None:
    """ "You have access to nothing" and "nothing matched" are different facts,
    and an empty answer would report the first as the second."""
    response = ask(slack.client, "U-STRANGER", "anything")

    assert response.status_code == 200
    assert "linked" in response.json()["text"]


def test_a_user_from_another_workspace_does_not_resolve(slack: Any) -> None:
    """Slack user ids are unique per workspace and nowhere else, so two
    workspaces in one install must not resolve to each other's people."""
    response = ask(slack.client, ALICE_SLACK, "anything", team_id="T-SOMEBODY-ELSE")

    assert "linked" in response.json()["text"]


def test_an_empty_command_explains_itself(slack: Any) -> None:
    response = ask(slack.client, ALICE_SLACK, "   ")

    assert "/hippo" in response.json()["text"]


def test_an_enormous_question_is_refused(slack: Any) -> None:
    response = ask(slack.client, ALICE_SLACK, "x" * 5000)

    assert "shorter question" in response.json()["text"]


# ---------------------------------------------------------------------------
# Approving, which is allowed here because a human clicks.
# ---------------------------------------------------------------------------


def press(client: TestClient, user: str, action_id: UUID, button: str) -> Any:
    payload = {
        "type": "block_actions",
        "team": {"id": TEAM},
        "user": {"id": user},
        "actions": [{"action_id": button, "value": str(action_id)}],
    }
    return post(client, "/api/v1/slack/interactions", {"payload": json.dumps(payload)})


def test_an_unsigned_interaction_is_rejected(slack: Any) -> None:
    """The endpoint that would otherwise let anybody approve anything."""
    response = slack.client.post("/api/v1/slack/interactions", content=b"payload=%7B%7D")

    assert response.status_code == 401


def test_a_person_can_approve_their_own_action(slack: Any) -> None:
    action_id = slack.propose(ALICE_SLACK)

    response = press(slack.client, ALICE_SLACK, action_id, "hippo_approve")

    assert "Approved" in response.json()["text"]
    assert slack.status_of(action_id) == "approved"


def test_a_person_cannot_approve_somebody_elses(slack: Any) -> None:
    """The check that makes an approve button safe. Without it, a signed
    request from any workspace member approves any action."""
    action_id = slack.propose(ALICE_SLACK)

    response = press(slack.client, BOB_SLACK, action_id, "hippo_approve")

    assert "cannot act on that" in response.json()["text"]
    assert slack.status_of(action_id) == "pending"


def test_refusal_looks_the_same_whatever_the_reason(slack: Any) -> None:
    """Somebody else's, already decided, or gone. A different answer per case
    would let anybody probe for what other people have proposed."""
    mine = slack.propose(ALICE_SLACK)
    press(slack.client, ALICE_SLACK, mine, "hippo_approve")

    answers = {
        press(slack.client, BOB_SLACK, mine, "hippo_approve").json()["text"],
        press(slack.client, ALICE_SLACK, mine, "hippo_approve").json()["text"],
        press(slack.client, ALICE_SLACK, uuid4(), "hippo_approve").json()["text"],
    }

    assert answers == {"I cannot act on that."}


def test_declining_changes_nothing_in_the_source(slack: Any) -> None:
    action_id = slack.propose(ALICE_SLACK)

    response = press(slack.client, ALICE_SLACK, action_id, "hippo_decline")

    assert "Nothing was changed" in response.json()["text"]
    assert slack.status_of(action_id) == "declined"


def test_approving_does_not_execute(slack: Any) -> None:
    """Rule 2 through a third surface. Approval marks a row; the sync worker,
    which is the only thing holding a Jira credential, executes it later."""
    action_id = slack.propose(ALICE_SLACK)

    press(slack.client, ALICE_SLACK, action_id, "hippo_approve")

    assert slack.status_of(action_id) == "approved"
    assert slack.executed_at(action_id) is None


def test_an_unlinked_user_cannot_press_anything(slack: Any) -> None:
    action_id = slack.propose(ALICE_SLACK)

    response = press(slack.client, "U-STRANGER", action_id, "hippo_approve")

    assert "linked" in response.json()["text"]
    assert slack.status_of(action_id) == "pending"


def test_a_button_with_no_action_id_is_ignored(slack: Any) -> None:
    response = post(slack.client, "/api/v1/slack/interactions", {"payload": json.dumps({})})

    assert "Nothing to do" in response.json()["text"]


def test_a_button_carrying_nonsense_is_ignored(slack: Any) -> None:
    payload = {
        "team": {"id": TEAM},
        "user": {"id": ALICE_SLACK},
        "actions": [{"action_id": "hippo_approve", "value": "not-a-uuid"}],
    }

    response = post(slack.client, "/api/v1/slack/interactions", {"payload": json.dumps(payload)})

    assert "do not recognise" in response.json()["text"]


def test_an_unparseable_payload_is_ignored(slack: Any) -> None:
    response = post(slack.client, "/api/v1/slack/interactions", {"payload": "{not json"})

    assert response.status_code == 200
    assert "Nothing to do" in response.json()["text"]


# ---------------------------------------------------------------------------
# Not mounted without a secret.
# ---------------------------------------------------------------------------


def test_the_surface_is_absent_without_a_signing_secret(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, not mounted-and-unguarded. A Slack endpoint that skips
    verification will approve a write into Jira for anybody who finds it."""
    from tests.slack_world import workspace

    with workspace(db_dsn, monkeypatch, "") as world:
        response = world.client.post("/api/v1/slack/commands", content=b"text=hi")

    assert response.status_code == 404


def test_the_openapi_document_shows_the_slack_routes_when_mounted(slack: Any) -> None:
    paths = slack.client.get("/openapi.json").json()["paths"]

    assert "/api/v1/slack/commands" in paths
    assert "/api/v1/slack/interactions" in paths


# ---------------------------------------------------------------------------
# Rendering.
# ---------------------------------------------------------------------------


def test_citations_are_rendered_as_links() -> None:
    from surfaces.slack.router import answer_blocks

    blocks = answer_blocks(
        "Legal review is the blocker. [1]",
        [{"marker": 1, "title": "ACME-1", "url": "https://acme.atlassian.net/browse/ACME-1"}],
    )

    assert blocks[0]["text"]["text"].startswith("Legal review")
    assert "<https://acme.atlassian.net/browse/ACME-1|ACME-1>" in blocks[1]["elements"][0]["text"]


def test_a_citation_without_a_link_still_renders() -> None:
    from surfaces.slack.router import answer_blocks

    blocks = answer_blocks("Answer.", [{"marker": 1, "title": "A note", "url": None}])

    assert "[1] A note" in blocks[1]["elements"][0]["text"]


def test_an_answer_with_no_citations_has_no_context_block() -> None:
    from surfaces.slack.router import answer_blocks

    assert len(answer_blocks("Nothing found.", [])) == 1


def test_a_proposal_says_nothing_has_happened_yet() -> None:
    """The person is about to decide. They should not be reading a message that
    implies it is already done."""
    from surfaces.slack.router import proposal_blocks

    blocks = proposal_blocks(uuid4(), "jira.comment", "Comment on ACME-1")

    assert "nothing has happened yet" in blocks[0]["text"]["text"]
    assert [element["action_id"] for element in blocks[1]["elements"]] == [
        "hippo_approve",
        "hippo_decline",
    ]


def test_a_long_answer_is_truncated_to_what_slack_renders() -> None:
    from surfaces.slack.router import answer_blocks

    blocks = answer_blocks("x" * 5000, [])

    assert len(blocks[0]["text"]["text"]) <= 3000


# ---------------------------------------------------------------------------
# The whole flow, in Slack: ask, propose, approve.
# ---------------------------------------------------------------------------


def test_a_request_comes_back_as_buttons_and_can_be_approved(
    db_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fragment's headline: ask, answer, approve, without leaving Slack.

    And the guarantee underneath it — approving marks a row. The sync worker,
    which is the only thing holding a Jira credential, executes it later.
    """
    from tests.slack_world import ProposingModel, workspace

    with workspace(db_dsn, monkeypatch, SECRET, ProposingModel()) as world:
        asked = ask(world.client, ALICE_SLACK, "add a comment on ACME-1 summarising this")
        body = asked.json()

        buttons = [
            element
            for block in body["blocks"]
            if block["type"] == "actions"
            for element in block["elements"]
        ]
        assert [element["action_id"] for element in buttons] == [
            "hippo_approve",
            "hippo_decline",
        ]
        assert body["response_type"] == "ephemeral"
        assert "nothing has happened yet" in json.dumps(body)

        action_id = UUID(buttons[0]["value"])
        assert world.status_of(action_id) == "pending"

        pressed = press(world.client, ALICE_SLACK, action_id, "hippo_approve")

        assert "Approved" in pressed.json()["text"]
        assert world.status_of(action_id) == "approved"
        assert world.executed_at(action_id) is None
