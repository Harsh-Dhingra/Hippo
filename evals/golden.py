"""The golden set: questions whose right answer someone decided by hand.

Small and labelled, rather than large and generated. A synthetic corpus can
only be as good as its generator, and a generator that knew which chunk answers
which question would be encoding the retrieval logic it was meant to measure.
These are the fixture corpus and questions a person would actually ask of it.

A case names the chunks that *must* come back, and — for the cases that matter
most — the chunks that must not. The second list is not a nicety: "what is
Acme asking for" is a reasonable question for Carol to ask, and the right
outcome is a thin answer rather than a good one.

Chunks are identified by a distinctive substring of their content. Ids would be
stable and unreadable; a substring makes a failure legible in the assertion
message, which is where a person is standing when they need it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

# Who can see what, in the fixture corpus:
#   alice, bob  — everything: #general, the private #deals-acme, all of Jira
#   carol       — #general and the PUB project only
ALICE = "U-ALICE"
BOB = "U-BOB"
CAROL = "U-CAROL"

# The eight chunks Carol must never reach, whatever she asks.
PRIVATE_TO_THE_DEAL = (
    "Acme is asking for 30 percent off",
    "our floor is 18 percent",
    "Acme renewal blocked on legal review",
    "Legal have flagged the liability cap",
    "legal will not sign until the liability cap",
    "engineering work is already finished",
    "Draft the 18 percent discount floor",
    "Our discount floor is eighteen percent",
)


class Case(BaseModel):
    """One labelled question."""

    model_config = ConfigDict(frozen=True)

    id: str
    question: str
    asker: str
    must_retrieve: tuple[str, ...]
    must_not_retrieve: tuple[str, ...] = ()
    # Why this case earns its place. A golden set without these becomes a set
    # of numbers nobody can interpret when one of them moves.
    tests: str


GOLDEN: tuple[Case, ...] = (
    Case(
        id="blocker-spans-both-systems",
        question="What is blocking the Acme renewal?",
        asker=ALICE,
        must_retrieve=(
            "legal review is the blocker",
            "Acme renewal blocked on legal review",
        ),
        tests="the demo question: the answer lives in Slack and Jira at once",
    ),
    Case(
        id="exact-identifier",
        question="What is ACME-1 about?",
        asker=ALICE,
        must_retrieve=("Acme renewal blocked on legal review",),
        tests="an identifier carries almost no semantic signal; this is the case "
        "hybrid retrieval exists for",
    ),
    Case(
        id="second-identifier",
        question="Summarise ACME-2",
        asker=ALICE,
        must_retrieve=("Draft the 18 percent discount floor",),
        tests="the same, for a ticket with no overlap with the question's words",
    ),
    Case(
        id="discount-asked-for",
        question="What discount is Acme asking for on the renewal?",
        asker=ALICE,
        must_retrieve=("Acme is asking for 30 percent off",),
        tests="content that exists only in the private channel",
    ),
    Case(
        id="our-floor",
        question="What is our discount floor?",
        asker=ALICE,
        must_retrieve=("Our discount floor is eighteen percent",),
        tests="the same fact stated in two systems; either is acceptable",
    ),
    Case(
        id="engineering-status",
        question="Is engineering finished on the renewal work?",
        asker=ALICE,
        must_retrieve=("engineering is done",),
        tests="a paraphrase: the corpus says 'done', the question asks 'finished'",
    ),
    Case(
        id="what-legal-flagged",
        question="What did legal flag on the renewal?",
        asker=ALICE,
        must_retrieve=("liability cap",),
        tests="a detail buried in a ticket description rather than a title",
    ),
    Case(
        id="guide-status",
        question="Is the integration guide live yet?",
        asker=CAROL,
        must_retrieve=("guide is live on the docs site",),
        tests="a question Carol can fully answer; the filtered path must not "
        "make her experience uniformly worse",
    ),
    Case(
        id="who-publishes",
        question="What is PUB-1?",
        asker=CAROL,
        must_retrieve=("Publish the integration guide",),
        tests="an identifier query from someone with narrow access",
    ),
    Case(
        id="jira-123-mention",
        question="What tracks the integration work?",
        asker=CAROL,
        must_retrieve=("JIRA-123 tracks the integration work",),
        tests="retrieval by description rather than by name",
    ),
    # -- the filtered path, as measured cases -------------------------------
    Case(
        id="carol-asks-the-demo-question",
        question="What is blocking the Acme renewal?",
        asker=CAROL,
        must_retrieve=("legal review is the blocker",),
        must_not_retrieve=PRIVATE_TO_THE_DEAL,
        tests="the demo. She gets the public half and none of the private half",
    ),
    Case(
        id="carol-asks-about-the-discount",
        question="What discount is Acme asking for on the renewal?",
        asker=CAROL,
        must_retrieve=(),
        must_not_retrieve=PRIVATE_TO_THE_DEAL,
        tests="a reasonable question she is not entitled to an answer to; the "
        "right outcome is nothing, not a hedge",
    ),
    Case(
        id="carol-names-the-private-ticket",
        question="What is ACME-1 about?",
        asker=CAROL,
        must_retrieve=(),
        must_not_retrieve=PRIVATE_TO_THE_DEAL,
        tests="naming a ticket directly must not be a way around the filter",
    ),
    Case(
        id="bob-sees-what-alice-sees",
        question="What is blocking the Acme renewal?",
        asker=BOB,
        must_retrieve=(
            "legal review is the blocker",
            "Acme renewal blocked on legal review",
        ),
        tests="access comes from channel membership, not from being the author",
    ),
)
