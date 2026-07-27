"""P3-GRF-2: the edges that make this a graph rather than two indexes.

Before this, Slack content and Jira content were connected only by words. "What
is blocking the Acme renewal" returned a thread and a ticket because both
contained "Acme" and "blocked", not because anything knew they were about the
same thing — which stops working the moment two projects share a vocabulary,
and stops working silently.

Most of these tests are about **direction**, because direction is where this is
easy to get wrong and expensive to get wrong. Two of the three relationships put
the far end in the source position:

  * an inward Jira "Blocks" link means *that* issue blocks this one
  * a pull request saying "fixes #123" means 123 was resolved *by* the request

Recording either backwards would answer "what is blocking this" with the things
it blocks — a confident, well-cited, exactly wrong answer.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from resolver.references import (
    BLOCKS,
    MAX_PER_RECORD,
    REFERENCES,
    RESOLVED_BY,
    closing_refs,
    from_jira_links,
    from_text,
    github_refs,
    jira_refs,
)
from sync.connectors.sdk import SourceRef

HERE = SourceRef(source_type="slack.message", source_id="C1:1")
ISSUE = SourceRef(source_type="jira.issue", source_id="ACME-1")


def relations(text: str, **kwargs: Any) -> list[tuple[str, str, str]]:
    return [
        (src.source_id, edge_type, dst.source_id)
        for src, dst, edge_type in from_text(HERE, text, **kwargs)
    ]


# ---------------------------------------------------------------------------
# Direction, which is the whole point.
# ---------------------------------------------------------------------------


def test_an_inward_block_names_the_far_issue_as_the_blocker() -> None:
    """The one that would be wrong in the most useful direction. Jira's inward
    link means *that* issue blocks this one."""
    payload = {
        "fields": {
            "issuelinks": [
                {"type": {"name": "Blocks"}, "inwardIssue": {"key": "ACME-9"}},
            ]
        }
    }

    found = from_jira_links(ISSUE, payload)

    assert [(s.source_id, d.source_id, t) for s, d, t in found] == [("ACME-9", "ACME-1", BLOCKS)]


def test_an_outward_block_names_this_issue_as_the_blocker() -> None:
    payload = {
        "fields": {
            "issuelinks": [
                {"type": {"name": "Blocks"}, "outwardIssue": {"key": "ACME-9"}},
            ]
        }
    }

    found = from_jira_links(ISSUE, payload)

    assert [(s.source_id, d.source_id, t) for s, d, t in found] == [("ACME-1", "ACME-9", BLOCKS)]


def test_a_pull_request_that_fixes_an_issue_is_the_resolver_not_the_resolved() -> None:
    """`resolved_by` reads "src was resolved by dst", so the issue is the
    source and "what fixed this" is the forward walk."""
    pull = SourceRef(source_type="github.issue", source_id="acme/web#7")

    found = from_text(pull, "This fixes #3 at last", repo="acme/web")

    assert [(s.source_id, d.source_id, t) for s, d, t in found] == [
        ("acme/web#3", "acme/web#7", RESOLVED_BY)
    ]


def test_a_plain_mention_runs_from_the_mentioner() -> None:
    assert relations("see ACME-1 for context") == [("C1:1", REFERENCES, "ACME-1")]


# ---------------------------------------------------------------------------
# What counts as a reference.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ACME-1 is blocked", ["ACME-1"]),
        ("see ACME-1 and PUB-22", ["ACME-1", "PUB-22"]),
        ("ACME-1, ACME-1, ACME-1", ["ACME-1"]),
        ("AB-1 is the shortest key", ["AB-1"]),
        ("", []),
    ],
)
def test_ticket_keys_are_found(text: str, expected: list[str]) -> None:
    assert jira_refs(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "the covid-19 numbers",
        "part number X-1 arrived",
        "acme-1 in lowercase",
        "ISO-8601 is a date format",
        "reference ABCDEFGHIJKL-1",
    ],
)
def test_things_that_look_like_keys_and_are_not(text: str) -> None:
    """A false positive here costs an edge into a ticket that does not exist,
    so this pattern is stricter than the one in resolver/curation.py — where a
    false positive costs a ranking nudge.

    ISO-8601 is the honest failure: it matches, and it is the price of a rule
    simple enough to explain. Resolution drops the edge because no such ticket
    exists, which is the safety net that makes a loose rule tolerable.
    """
    found = jira_refs(text)
    assert found in ([], ["ISO-8601"]), text


def test_a_qualified_github_reference_is_found() -> None:
    assert github_refs("see acme/web#12") == ["acme/web#12"]


def test_a_bare_number_only_counts_inside_a_repository() -> None:
    """Outside one, "#1" is far more often a numbered list item than an issue,
    and a wrong edge is worse than a missing one."""
    assert github_refs("see #12") == []
    assert github_refs("see #12", repo="acme/web") == ["acme/web#12"]


@pytest.mark.parametrize(
    "keyword", ["closes", "closed", "fixes", "fixed", "fix", "resolves", "resolved"]
)
def test_every_closing_keyword_github_honours(keyword: str) -> None:
    assert closing_refs(f"{keyword} #4", repo="acme/web") == ["acme/web#4"]


def test_closing_is_case_insensitive() -> None:
    assert closing_refs("Fixes #4", repo="acme/web") == ["acme/web#4"]


def test_a_closing_reference_is_not_also_a_plain_one() -> None:
    """ "fixes #123" should produce one strong relationship, not that plus a
    weaker duplicate of it."""
    found = relations("fixes #4", repo="acme/web", github_type="github.issue")

    assert len(found) == 1
    assert found[0][1] == RESOLVED_BY


def test_a_message_can_reference_both_systems_at_once() -> None:
    found = relations("ACME-1 and acme/web#9 are related")

    assert ("C1:1", REFERENCES, "ACME-1") in found
    assert ("C1:1", REFERENCES, "acme/web#9") in found


# ---------------------------------------------------------------------------
# Link types.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Blocks", "blocks", "is blocked by", "Blocked by"])
def test_the_blocking_link_names_are_recognised(name: str) -> None:
    payload = {"fields": {"issuelinks": [{"type": {"name": name}, "outwardIssue": {"key": "X-1"}}]}}

    assert from_jira_links(ISSUE, payload)[0][2] == BLOCKS


@pytest.mark.parametrize("name", ["Relates", "Duplicates", "Cloners", "Some Custom Type"])
def test_any_other_link_type_is_a_plain_reference(name: str) -> None:
    """Jira lets an administrator rename these. An unrecognised name becomes a
    reference rather than a guess at what it meant."""
    payload = {"fields": {"issuelinks": [{"type": {"name": name}, "outwardIssue": {"key": "X-1"}}]}}

    assert from_jira_links(ISSUE, payload)[0][2] == REFERENCES


def test_a_malformed_link_is_skipped_rather_than_raising() -> None:
    """Schema drift never loses a record and never stops a resolver pass."""
    payload = {
        "fields": {
            "issuelinks": [
                "not a dict",
                {},
                {"type": {}},
                {"type": {"name": "Blocks"}},
                {"type": {"name": "Blocks"}, "outwardIssue": {"key": "X-1"}},
            ]
        }
    }

    assert len(from_jira_links(ISSUE, payload)) == 1


def test_an_issue_with_no_links_produces_nothing() -> None:
    assert from_jira_links(ISSUE, {}) == []
    assert from_jira_links(ISSUE, {"fields": {}}) == []
    assert from_jira_links(ISSUE, {"fields": {"issuelinks": None}}) == []


# ---------------------------------------------------------------------------
# Bounds.
# ---------------------------------------------------------------------------


def test_one_record_cannot_claim_a_hundred_relationships() -> None:
    """A release note quoting fifty tickets is real, and so is somebody
    deciding their message should appear in every answer. The cap handles both
    without needing to tell them apart."""
    text = " ".join(f"ACME-{index}" for index in range(1, 60))

    assert len(relations(text)) == MAX_PER_RECORD


def test_the_cap_applies_to_jira_links_too() -> None:
    payload = {
        "fields": {
            "issuelinks": [
                {"type": {"name": "Relates"}, "outwardIssue": {"key": f"X-{index}"}}
                for index in range(50)
            ]
        }
    }

    assert len(from_jira_links(ISSUE, payload)) == MAX_PER_RECORD


def test_a_record_never_references_itself() -> None:
    """A self-edge says nothing and makes the graph walk loop."""
    found = from_text(ISSUE, "ACME-1 is this very ticket", jira_type="jira.issue")

    assert all((src, dst) != (ISSUE, ISSUE) for src, dst, _ in found)


# ---------------------------------------------------------------------------
# The done-condition, as a query.
# ---------------------------------------------------------------------------


@pytest.mark.requires_db
def test_what_is_blocking_this_is_answered_by_a_hop(migrated: Any) -> None:
    """The fragment, stated as the question the product exists for.

    The blocker shares not one word with the question. Before P3-GRF-2 nothing
    connected them and the only way to find it was to already know its words.
    """
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings

    alice = uuid4()
    migrated.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (alice,))

    def ticket(title: str, body: str) -> UUID:
        entity = uuid4()
        migrated.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'ticket', %s)",
            (entity, title),
        )
        migrated.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
            (entity, alice),
        )
        migrated.execute(
            "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
            "VALUES (%s, '00000000-0000-0000-0000-000000000001', %s, 0)",
            (entity, body),
        )
        return entity

    renewal = ticket("ACME-1", "Acme renewal for the coming year")
    blocker = ticket("LEGAL-7", "counsel will not sign the indemnity wording")
    migrated.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'blocks', 'source')",
        (blocker, renewal),
    )

    hits = retrieve(
        migrated,
        alice,
        RetrievalPlan(query_text="Acme renewal", k=10, hops=1),
        HashingEmbeddings(),
    )

    contents = [hit.content for hit in hits]
    assert "counsel will not sign the indemnity wording" in contents
    reached = next(hit for hit in hits if "counsel" in hit.content)
    assert "graph" in reached.retrieval_modes
