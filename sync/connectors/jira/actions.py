"""What the Jira connector can be asked to do.

Its own module, with no transport import, because two very different processes
need it. The sync worker needs it to execute; the agent needs it to know what
may be proposed at all — and the agent must never import a module that can hold
a Jira credential.

Payload models forbid extra fields. That is the check that stops a proposal
carrying something the connector would pass through to Jira unexamined, and it
is why validation happens here rather than at the HTTP call.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sync.connectors.sdk import ActionDefinition

COMMENT_ACTION = "jira.comment"
TRANSITION_ACTION = "jira.transition"

ISSUE = "jira.issue"


class CommentPayload(BaseModel):
    """jira.comment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str = Field(min_length=1, max_length=32_000)


class TransitionPayload(BaseModel):
    """jira.transition."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    to_status: str = Field(min_length=1, max_length=200)


ACTIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        action_type=COMMENT_ACTION,
        description="Add a comment to a Jira issue.",
        targets=frozenset({ISSUE}),
        payload_schema='{"body": "the comment text"}',
        payload_model=CommentPayload,
    ),
    ActionDefinition(
        action_type=TRANSITION_ACTION,
        description="Move a Jira issue to another status.",
        targets=frozenset({ISSUE}),
        payload_schema='{"to_status": "the target status name"}',
        payload_model=TransitionPayload,
    ),
)
