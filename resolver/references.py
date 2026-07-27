"""Cross-references, which are what make this a graph rather than two indexes.

Until now Slack content and Jira content were connected only by words. Ask "what
is blocking the Acme renewal" and both a thread and a ticket come back because
both happen to contain "Acme" and "blocked" — not because anything in the system
knows they are about the same thing. On a corpus where two projects both use the
word "renewal", that stops working, and nothing announces it.

A message that says `ACME-1` is stating a relationship. So is a Jira issue link
that says "is blocked by". So is a pull request that says "fixes #123". All
three are things the source already recorded, which is why none of this needs a
model: P3-RES-1 put a fence around model-inferred identity and there is no
reason to climb it for facts somebody typed on purpose.

**Three relationships, and the directions were chosen to answer questions.**

    blocks       src blocks dst. Reverse — "what is blocking this" — is the
                 question this product exists to answer, so both directions
                 are weighted highly in migration 024.
    resolved_by  src was resolved by dst. Forward is "what fixed this",
                 reverse is "what did this fix".
    references   src mentions dst. Forward is the strong direction; reverse
                 reaches everything that mentioned a ticket, which is useful
                 and bounded by the fan-out cap rather than by luck.

**A reference is a claim by whoever wrote the text.** Somebody who can post in a
channel you read can put `ACME-1` in a message and make it reachable from
questions about that ticket. That is what mentioning is, and the bound is the
one that always applies: the message still only surfaces for people who could
already read it. `MAX_PER_RECORD` stops one message from claiming a hundred
relationships, which is the only version of this worth calling abuse.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator
from typing import Any

from sync.connectors.sdk import SourceRef

LOG = logging.getLogger("hippo.resolver.references")

BLOCKS = "blocks"
RESOLVED_BY = "resolved_by"
REFERENCES = "references"

# A message quoting a hundred ticket keys is a release note or a bot, and
# either way the hundredth reference says nothing. Also the bound on somebody
# deciding their message should appear in every answer.
MAX_PER_RECORD = 10

# A Jira key: a project code, a hyphen, a number. Deliberately stricter than
# the identifier pattern in resolver/curation.py, which only has to decide
# whether a short message is worth keeping — a false positive there costs a
# ranking nudge, and here it costs an edge into a ticket that does not exist.
JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")

# owner/repo#123, the form GitHub uses for a cross-repository reference.
GITHUB_QUALIFIED = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d{1,7})\b")

# A bare #123, which only means anything inside a record that already knows its
# repository. Not matched anywhere else, because "#1" in a Slack message is far
# more often a place in a list than an issue.
GITHUB_BARE = re.compile(r"(?:^|\s)#(\d{1,7})\b")

# GitHub's own closing keywords. A pull request saying any of these is stating
# that merging it resolves the issue, which is a stronger claim than a mention.
CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:([\w.-]+/[\w.-]+)#|#)(\d{1,7})\b",
    re.IGNORECASE,
)

# Jira link type names that mean blocking, lowercased. Jira lets an
# administrator rename these, so the match is on the name Jira ships with and
# anything unrecognised falls back to a plain reference rather than being
# guessed at.
BLOCKING_LINKS = frozenset({"blocks", "is blocked by", "blocked by"})


def jira_refs(text: str) -> list[str]:
    """Ticket keys mentioned in a piece of text, in order, deduplicated."""
    seen: dict[str, None] = {}
    for project, number in JIRA_KEY.findall(text or ""):
        seen.setdefault(f"{project}-{number}", None)
    return list(seen)[:MAX_PER_RECORD]


def github_refs(text: str, repo: str | None = None) -> list[str]:
    """Issue references, as `owner/repo#number`.

    A bare `#123` resolves against `repo` when one is known and is ignored
    otherwise: outside a repository it is far more often a numbered list item
    than an issue, and a wrong edge is worse than a missing one.
    """
    seen: dict[str, None] = {}
    for owner_repo, number in GITHUB_QUALIFIED.findall(text or ""):
        seen.setdefault(f"{owner_repo}#{number}", None)
    if repo:
        for number in GITHUB_BARE.findall(text or ""):
            seen.setdefault(f"{repo}#{number}", None)
    return list(seen)[:MAX_PER_RECORD]


def closing_refs(text: str, repo: str | None = None) -> list[str]:
    """Issues a pull request says it closes."""
    seen: dict[str, None] = {}
    for owner_repo, number in CLOSING.findall(text or ""):
        target = owner_repo or repo
        if target:
            seen.setdefault(f"{target}#{number}", None)
    return list(seen)[:MAX_PER_RECORD]


# (src, dst, edge_type). Complete triples rather than "a target for this
# record", because two of the three relationships put the *other* end in the
# source position: an inward Jira block is "that issue blocks this one", and a
# pull request that fixes an issue means the issue was resolved by the request.
# Returning only a target would have forced both to be recorded backwards.
Relation = tuple[SourceRef, SourceRef, str]


def _usable(items: Iterable[Relation], where: str) -> Iterator[Relation]:
    """Drop self-edges, then yield at most MAX_PER_RECORD.

    A ticket whose description names its own key is common and means nothing;
    the edge would be a self-loop that makes the graph walk circle. Resolution
    drops those too, but emitting one and relying on that would be leaving a
    known-wrong row for somebody else to catch.

    The cap says so when it truncates. Silently dropping the eleventh would
    make a release note look like it referenced ten things on purpose.
    """
    kept = 0
    for src, dst, edge_type in items:
        if src == dst:
            continue
        if kept >= MAX_PER_RECORD:
            LOG.info("truncated cross-references", extra={"record": where, "kept": kept})
            return
        kept += 1
        yield (src, dst, edge_type)


def from_text(
    source: SourceRef,
    text: str,
    *,
    jira_type: str = "jira.issue",
    github_type: str = "github.issue",
    repo: str | None = None,
) -> list[Relation]:
    """Every relationship a piece of prose states.

    Closing keywords are checked first and their targets excluded from the
    plain-reference pass, so "fixes #123" produces one `resolved_by` rather
    than that plus a weaker `references` to the same issue.

    Note the direction on `resolved_by`: the issue is the source. A pull
    request saying "fixes #123" states that 123 was resolved by it, so the edge
    runs issue -> request and "what fixed this" is the forward walk.
    """
    resolved = closing_refs(text, repo)
    found: list[Relation] = [
        (SourceRef(source_type=github_type, source_id=target), source, RESOLVED_BY)
        for target in resolved
    ]
    found.extend(
        (source, SourceRef(source_type=github_type, source_id=target), REFERENCES)
        for target in github_refs(text, repo)
        if target not in resolved
    )
    found.extend(
        (source, SourceRef(source_type=jira_type, source_id=key), REFERENCES)
        for key in jira_refs(text)
    )
    return list(_usable(found, str(source.source_id)))


def from_jira_links(source: SourceRef, payload: dict[str, Any]) -> list[Relation]:
    """Jira's own issue links, which are the strongest dependency signal there is.

    Jira states direction with two fields: `outwardIssue` is the far end when
    this issue is the subject of the link name, `inwardIssue` when it is the
    object. For the "Blocks" type an outward link means *this issue blocks that
    one*, and an inward link means *that one blocks this*. Getting it backwards
    would answer "what is blocking this" with the things it blocks.

    The inward case is recorded with the far issue as the source rather than
    left for that issue's own record to state. Waiting would be correct only
    when both ends are synced, and the case that matters most — a renewal
    blocked by something in a project nobody syncs — is exactly the case where
    they are not.
    """
    links = payload.get("fields", {}).get("issuelinks") or []
    found: list[Relation] = []

    for link in links:
        if not isinstance(link, dict):
            continue
        name = str((link.get("type") or {}).get("name", "")).strip().lower()
        outward = (link.get("outwardIssue") or {}).get("key")
        inward = (link.get("inwardIssue") or {}).get("key")

        for key, is_outward in ((outward, True), (inward, False)):
            if not key:
                continue
            far = SourceRef(source_type="jira.issue", source_id=str(key))
            if name in BLOCKING_LINKS:
                # `blocks` reads "src blocks dst", so the outward link keeps
                # this issue as the source and the inward one swaps the ends.
                found.append((source, far, BLOCKS) if is_outward else (far, source, BLOCKS))
            else:
                found.append((source, far, REFERENCES))

    return list(_usable(found, str(source.source_id)))
