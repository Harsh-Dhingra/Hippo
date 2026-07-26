"""What the GitHub connector can be asked to do.

Its own module with no transport import: the sync worker needs it to execute
and the agent needs it to know what may be proposed, and the agent must never
import a module that can hold a credential.

Two actions, and the second one is the reason this connector is worth writing.
`github.close` is the first write-back whose inverse is not "delete the thing we
made" — closing an issue is undone by reopening it, which means the rollback
path depends on state captured beforehand rather than on an id returned
afterwards. If the SDK only fitted append-shaped actions, this is where that
would show.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from sync.connectors.sdk import ActionDefinition

COMMENT_ACTION = "github.comment"
CLOSE_ACTION = "github.close"

ISSUE = "github.issue"


class CommentPayload(BaseModel):
    """github.comment."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str = Field(min_length=1, max_length=65_536)


class ClosePayload(BaseModel):
    """github.close.

    `reason` is GitHub's own vocabulary and is constrained to it here rather
    than passed through: a free string would be rejected by the API after a
    person had already approved the action.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: Literal["completed", "not_planned"] = "completed"


ACTIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        action_type=COMMENT_ACTION,
        description="Add a comment to a GitHub issue or pull request.",
        targets=frozenset({ISSUE}),
        payload_schema='{"body": "the comment text"}',
        payload_model=CommentPayload,
    ),
    ActionDefinition(
        action_type=CLOSE_ACTION,
        description="Close a GitHub issue or pull request.",
        targets=frozenset({ISSUE}),
        payload_schema='{"reason": "completed" or "not_planned"}',
        payload_model=ClosePayload,
    ),
)
