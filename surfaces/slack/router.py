"""Asking Hippo from Slack, and approving from Slack.

The fragment says ask, answer, approve. Two of those are ordinary and one is
the interesting one, so the interesting one first.

**Approving here is fine, and approving from the MCP surface is not.** The
difference is not the protocol, it is who clicks. Slack's interactive payload
names the human who pressed the button, and this checks that they are the
person the action belongs to. An MCP tool has no such person — the caller is a
model, and a model that could approve its own proposal would make rule 2
decorative. So this surface has an approve button and that one does not.

**An answer goes back to the asker, never to the channel.** This is the failure
mode that would otherwise arrive dressed as a feature. Hippo filters an answer
to what the asking person may read; posting it into #general shows it to
everybody in #general, some of whom may not be entitled to any of it. Every
response here is ephemeral, and `response_type` is not a parameter anybody can
pass.

**A Slack user is a principal or they are nobody.** The identity is the Slack
user id the workspace signed, mapped to the principal the connector synced with
that source id. There is no fallback, no shared account and no "workspace
default" — an unlinked user gets told they are unlinked, which is a true
statement about their access rather than an empty answer that reads like one.

The router is mounted in the API process because it needs the agent and the
principal lookup. It adds no read path: a query runs under SET LOCAL ROLE
hippo_agent through visible_chunks(), exactly as the REST route does.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from agent.loop import Agent
from api.approvals import ActionConflictError, ActionNotFoundError, approve, decline
from core.db import Connection
from surfaces.slack.signing import SignatureError, verify

LOG = logging.getLogger("hippo.surfaces.slack")

# Slack renders this to the person who typed the command and to nobody else.
# Not configurable, and not a parameter: see the module docstring.
EPHEMERAL = "ephemeral"

MAX_QUESTION = 2000


class SlackError(Exception):
    """Something to tell the person in Slack."""


def _say(text: str, blocks: list[dict[str, Any]] | None = None) -> JSONResponse:
    """A reply only the person who asked can see."""
    body: dict[str, Any] = {"response_type": EPHEMERAL, "text": text}
    if blocks:
        body["blocks"] = blocks
    return JSONResponse(body)


def principal_for(conn: Connection, team_id: str, user_id: str) -> UUID:
    """The principal behind a Slack user id.

    Scoped to the connector for that workspace, so two workspaces in one
    install cannot resolve to each other's people — Slack user ids are unique
    per workspace and nowhere else.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id FROM principals p "
            "JOIN connectors c ON c.id = p.connector_id "
            "WHERE c.kind = 'slack' AND p.kind = 'user' AND p.source_id = %s "
            "  AND (c.config ->> 'team_id' IS NULL OR c.config ->> 'team_id' = %s) "
            "ORDER BY p.id LIMIT 1",
            (user_id, team_id),
        )
        row = cur.fetchone()

    if row is None:
        raise SlackError(
            "I do not have a Hippo account linked to your Slack user yet, so there is "
            "nothing I can show you. An administrator links it after the next sync."
        )
    return UUID(str(row[0]))


def answer_blocks(text: str, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The answer, with its sources as links."""
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}
    ]
    if citations:
        lines = []
        for citation in citations:
            title = citation.get("title") or citation.get("entity_type")
            url = citation.get("url")
            marker = citation.get("marker")
            lines.append(f"[{marker}] <{url}|{title}>" if url else f"[{marker}] {title}")
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(lines)}]}
        )
    return blocks


def proposal_blocks(action_id: UUID, action_type: str, summary: str) -> list[dict[str, Any]]:
    """A pending action, with the two buttons a person can press.

    The action id rides in `value` and is checked against the clicker on the
    way back in. It is not a secret — it appears in the UI too — and it is not
    what makes approving safe; the ownership check is.
    """
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Proposed:* {summary}\n_{action_type}_ — nothing has happened yet.",
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Approve"},
                    "action_id": "hippo_approve",
                    "value": str(action_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Decline"},
                    "action_id": "hippo_decline",
                    "value": str(action_id),
                },
            ],
        },
    ]


def build_router(
    connection: Callable[[], AbstractContextManager[Connection]],
    agent: Callable[[Connection], Agent],
    as_agent: Callable[[Connection], Any],
    signing_secret: str,
) -> APIRouter:
    """The two endpoints Slack calls.

    Dependencies are passed in rather than imported so this stays a surface:
    it borrows the API's connection pool and agent and owns neither.

    `connection` is a context manager rather than a generator on purpose. A
    generator iterated with `for conn in connection(): ... return` is abandoned
    suspended, finalised later with GeneratorExit, and psycopg treats that as a
    failure and rolls back — so an approval would answer "Approved" and change
    nothing. It is exactly the kind of bug that passes a smoke test.
    """
    router = APIRouter(prefix="/slack", tags=["slack"])

    async def checked(request: Request, timestamp: str | None, signature: str | None) -> bytes:
        """The raw body, once it is known to be Slack's.

        Raw because Slack signed the bytes it sent. Re-serialising a parsed
        form produces a different string — a different field order, a different
        encoding of a space — and the mismatch is intermittent rather than
        total, which is the worst kind.
        """
        body = await request.body()
        verify(signing_secret, signature, timestamp, body)
        return body

    @router.post("/commands", summary="A slash command from Slack")
    async def slash_command(
        request: Request,
        x_slack_signature: str | None = Header(default=None),
        x_slack_request_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        """`/hippo what is blocking the renewal`.

        The answer comes back ephemerally. It is filtered to the person who
        typed the command, and posting it into the channel would show it to
        everybody in the channel — some of whom may be entitled to none of it.
        """
        try:
            body = await checked(request, x_slack_request_timestamp, x_slack_signature)
        except SignatureError as exc:
            LOG.warning("rejected a Slack command", extra={"reason": str(exc)})
            return JSONResponse({"error": "unauthorised"}, status_code=401)

        form = _form(body)
        question = str(form.get("text", "")).strip()
        if not question:
            return _say("Ask me something: `/hippo what is blocking the Acme renewal`")
        if len(question) > MAX_QUESTION:
            return _say(f"That is longer than {MAX_QUESTION} characters. Try a shorter question.")

        with connection() as conn:
            try:
                principal_id = principal_for(
                    conn, str(form.get("team_id", "")), str(form.get("user_id", ""))
                )
            except SlackError as exc:
                return _say(str(exc))

            built = agent(conn)
            with as_agent(conn):
                answer = built.answer(conn, principal_id, question, k=12)

            blocks = answer_blocks(
                answer.text,
                [
                    {
                        "marker": citation.marker,
                        "title": citation.title or citation.entity_type,
                        "url": citation.url,
                    }
                    for citation in answer.citations
                ],
            )
            if answer.proposal is not None:
                blocks.extend(
                    proposal_blocks(
                        answer.proposal.id,
                        answer.proposal.action_type,
                        answer.proposal.summary,
                    )
                )
            return _say(answer.text[:200], blocks)

    @router.post("/interactions", summary="A button press from Slack")
    async def interaction(
        request: Request,
        x_slack_signature: str | None = Header(default=None),
        x_slack_request_timestamp: str | None = Header(default=None),
    ) -> JSONResponse:
        """Approve or decline, pressed by a person.

        This is the endpoint that separates the Slack surface from the MCP one.
        Slack names the human who clicked; the ownership check below is what
        makes it a human approving their own action rather than anybody
        approving anything.
        """
        try:
            body = await checked(request, x_slack_request_timestamp, x_slack_signature)
        except SignatureError as exc:
            LOG.warning("rejected a Slack interaction", extra={"reason": str(exc)})
            return JSONResponse({"error": "unauthorised"}, status_code=401)

        payload = _payload(body)
        actions = payload.get("actions") or []
        if not actions:
            return _say("Nothing to do.")

        pressed = actions[0]
        action_id = _uuid(str(pressed.get("value", "")))
        if action_id is None:
            return _say("I do not recognise that button.")

        team = str((payload.get("team") or {}).get("id", ""))
        who = str((payload.get("user") or {}).get("id", ""))

        with connection() as conn:
            try:
                principal_id = principal_for(conn, team, who)
            except SlackError as exc:
                return _say(str(exc))

            # approve() and decline() check ownership themselves, against the
            # same _same_person() expansion the UI uses. Doing it again here
            # would be a second implementation of the rule that matters.
            try:
                if pressed.get("action_id") == "hippo_approve":
                    approve(conn, principal_id, action_id)
                    return _say("Approved. It will be executed shortly.")
                decline(conn, principal_id, action_id)
                return _say("Declined. Nothing was changed.")
            except (ActionNotFoundError, ActionConflictError) as exc:
                # One message whether it is somebody else's action, already
                # decided, or gone. api/approvals.py already refuses to
                # distinguish the first two; saying which here would undo that
                # and let anybody probe for what other people have proposed.
                LOG.info("slack approval refused", extra={"reason": str(exc)})
                return _say("I cannot act on that.")

    return router


def _form(body: bytes) -> dict[str, str]:
    from urllib.parse import parse_qs

    return {key: values[0] for key, values in parse_qs(body.decode()).items()}


def _payload(body: bytes) -> dict[str, Any]:
    """Slack sends interactions as a form field holding JSON."""
    import json

    raw = _form(body).get("payload", "{}")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except ValueError:
        return None
