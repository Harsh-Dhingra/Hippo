"""Chunking policies: what part of a source object is worth retrieving.

Per-message for Slack, per-field for Jira (ARCHITECTURE section 6). The policy
is deterministic and per source type, so the same payload always produces the
same chunks with the same hashes, which is what lets a re-run leave unchanged
text and its embedding alone.

Chunks come from the raw payload rather than from the entity, because the
entity keeps only a title. A Jira issue's description is the substance of the
ticket and lives nowhere else.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict

# Long enough that a normal message or comment is one chunk, short enough that
# a retrieved chunk is mostly signal. Tuned properly against the eval harness
# in P2-EVAL-1; these are defaults, not findings.
MAX_CHARS = 1500
OVERLAP = 200

# Roughly four characters per token for English prose. An estimate, used for
# budgeting a prompt rather than for billing, and cheaper than a tokeniser
# dependency for what it buys.
CHARS_PER_TOKEN = 4


class Chunk(BaseModel):
    """One embeddable unit, identified by what it says."""

    model_config = ConfigDict(frozen=True)

    content: str
    index: int

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    @property
    def token_estimate(self) -> int:
        return max(1, len(self.content) // CHARS_PER_TOKEN)


def adf_text(node: Any) -> str:
    """Flatten Atlassian Document Format to plain text.

    Real Jira sends rich text as a document tree. Walking it is deterministic
    and belongs here rather than in the connector, which stores the payload
    verbatim and interprets nothing.
    """
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(part for part in (adf_text(item) for item in node) if part)
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            return str(node["text"])
        return adf_text(node.get("content", []))
    return ""


def split_text(text: str, max_chars: int = MAX_CHARS, overlap: int = OVERLAP) -> list[str]:
    """Break long text into overlapping windows, on word boundaries.

    Overlap exists so a sentence spanning a boundary is retrievable from both
    sides rather than from neither.
    """
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    if len(cleaned) <= max_chars:
        return [cleaned]

    windows: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(start + max_chars, len(cleaned))
        if end < len(cleaned):
            # Back off to whitespace rather than cutting a word in half. Never
            # back off past the midpoint, or a long unbroken run of text would
            # produce absurdly small windows.
            cut = cleaned.rfind(" ", start + max_chars // 2, end)
            if cut > start:
                end = cut
        window = cleaned[start:end].strip()
        if window:
            windows.append(window)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return windows


# ---------------------------------------------------------------------------
# Policies. One per source type; anything not listed is not chunked.
# ---------------------------------------------------------------------------


def _slack_message_fields(payload: Mapping[str, Any]) -> list[str]:
    return [str(payload.get("text") or "")]


def _jira_issue_fields(payload: Mapping[str, Any]) -> list[str]:
    """Per field. A summary and a description answer different questions, and
    fusing them into one chunk buries the shorter one."""
    fields = payload.get("fields") or {}
    return [str(fields.get("summary") or ""), adf_text(fields.get("description"))]


def _jira_comment_fields(payload: Mapping[str, Any]) -> list[str]:
    return [adf_text(payload.get("body"))]


FieldExtractor = Callable[[Mapping[str, Any]], list[str]]

POLICIES: dict[str, FieldExtractor] = {
    "slack.message": _slack_message_fields,
    "jira.issue": _jira_issue_fields,
    "jira.comment": _jira_comment_fields,
}

# Source types that are containers or people rather than things anyone asks
# about. Chunking them would put rows in the retrieval path that no answer
# would ever cite.
NOT_CHUNKED = frozenset(
    {"slack.channel", "slack.user", "slack.group", "jira.project", "jira.user", "jira.group"}
)


def chunks_for(
    source_type: str,
    payload: Mapping[str, Any],
    *,
    max_chars: int = MAX_CHARS,
    overlap: int = OVERLAP,
    start_index: int = 0,
) -> list[Chunk]:
    """The chunks one source object contributes. Deterministic."""
    policy = POLICIES.get(source_type)
    if policy is None:
        return []

    chunks: list[Chunk] = []
    index = start_index
    for field in policy(payload):
        for window in split_text(field, max_chars=max_chars, overlap=overlap):
            chunks.append(Chunk(content=window, index=index))
            index += 1
    return chunks
