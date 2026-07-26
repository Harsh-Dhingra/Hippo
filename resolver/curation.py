"""Deciding which memories deserve a retrieval slot.

PROJECT.md calls this the gap in everyone else. The reason it stays a gap is
that the obvious implementation is deletion, and deletion is wrong: a system
that quietly stops answering questions it could answer leaves the person asking
unable to tell "we do not have that" from "we decided it was stale".

So nothing here removes anything. Every output is a weight the permission
filter multiplies into a score (migration 020), and a chunk scored zero still
comes back when nothing better exists.

**Noise is a shape, not a topic.** "+1", "thanks", "lunch order going in at
noon" — real messages that will never answer a question. The rules below key on
length, on distinctive vocabulary, and on how many times the same text appears
across the corpus, because those are decidable. Nothing here tries to judge
whether content is *important*, which is not.

**Supersede needs evidence, not a guess.** Two chunks state the same fact and
one is newer, so the newer wins — but only when they are near-identical
restatements from the same container. A model could do better and P3-RES-1 is
where that belongs; guessing here would demote a correct answer in favour of a
later, vaguer one, and that failure is invisible.

The pass is idempotent and re-runnable. curated_at records what has been looked
at, so a large corpus can be worked through in batches and a re-run after a
rule change simply reassesses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from prometheus_client import Counter

from core.db import Connection

LOG = logging.getLogger("hippo.resolver.curation")

DEMOTED = Counter("hippo_curation_demoted_total", "Chunks demoted.", ("reason",))

# Content this short says nothing on its own whatever it contains.
TRIVIAL_LENGTH = 12
SHORT_LENGTH = 40

# Acknowledgements. Matched whole, so "thanks for the detailed writeup on the
# caching layer" is not caught — it is the bare acknowledgement that is noise.
ACKNOWLEDGEMENTS = frozenset(
    {
        "ok",
        "okay",
        "k",
        "kk",
        "yes",
        "no",
        "yep",
        "yup",
        "nope",
        "sure",
        "thanks",
        "thank you",
        "ta",
        "cheers",
        "np",
        "no problem",
        "welcome",
        "done",
        "agreed",
        "agree",
        "same",
        "this",
        "lgtm",
        "ack",
        "+1",
        "-1",
        "nice",
        "great",
        "cool",
        "awesome",
        "perfect",
        "lol",
        "haha",
        "morning",
        "good morning",
        "hi",
        "hello",
        "hey",
        "bye",
        "on it",
        "will do",
        "sounds good",
        "makes sense",
        "got it",
    }
)

# Distinctive enough to be worth keeping however short. An identifier is the
# whole point of the exact-identifier case in the eval harness.
IDENTIFIER = re.compile(r"\b[A-Z][A-Z0-9]{1,}-[A-Za-z0-9.]+\b")
URL = re.compile(r"https?://")

# Weights rather than a boolean, so ordinary content stays at 1.0 and only
# things nothing could ever ask for reach the floor.
SIGNAL_NOISE = 0.05
SIGNAL_LOW = 0.35
SIGNAL_DUPLICATE = 0.2


@dataclass
class CurationStats:
    """What one pass did."""

    examined: int = 0
    noise: int = 0
    low: int = 0
    duplicate: int = 0
    superseded: int = 0

    @property
    def demoted(self) -> int:
        return self.noise + self.low + self.duplicate


def score_signal(content: str, *, copies: int = 1) -> float:
    """How likely this text is to ever answer a question.

    Deliberately conservative and deliberately explainable. Every branch here
    is something a person would agree with on sight, because a curation rule
    nobody can predict is one nobody will trust with their corpus.
    """
    stripped = content.strip()
    folded = stripped.lower().strip(".!? ")

    if not stripped:
        return SIGNAL_NOISE
    if folded in ACKNOWLEDGEMENTS:
        return SIGNAL_NOISE
    # Emoji, punctuation, a bare reaction: nothing a query could match.
    if not any(character.isalnum() for character in stripped):
        return SIGNAL_NOISE
    if len(stripped) <= TRIVIAL_LENGTH and not IDENTIFIER.search(stripped):
        return SIGNAL_NOISE

    # The same sentence in fifty places is boilerplate — a signature, a bot
    # footer, a standing reminder. One copy would be worth keeping; fifty
    # crowding out a real answer is not.
    if copies >= 10:
        return SIGNAL_DUPLICATE

    if len(stripped) <= SHORT_LENGTH and not (IDENTIFIER.search(stripped) or URL.search(stripped)):
        return SIGNAL_LOW

    return 1.0


def curate(conn: Connection, connector_id: UUID | None = None, limit: int = 5000) -> CurationStats:
    """Assess chunks that have not been assessed, and weight them.

    Idempotent: curated_at records what has been looked at, so this can be run
    on a schedule over a large corpus and re-run after a rule change without
    doing anything twice by accident.
    """
    stats = CurationStats()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.content, "
            "       count(*) OVER (PARTITION BY c.content_hash) AS copies "
            "FROM chunks c "
            "JOIN entities e ON e.id = c.entity_id "
            "LEFT JOIN entity_sources es ON es.entity_id = e.id "
            "LEFT JOIN raw_records r ON r.id = es.raw_record_id "
            "WHERE c.curated_at IS NULL "
            "  AND (%s::uuid IS NULL OR r.connector_id = %s) "
            "LIMIT %s",
            (connector_id, connector_id, max(1, min(limit, 50_000))),
        )
        rows = cur.fetchall()

    for chunk_id, content, copies in rows:
        signal = score_signal(str(content), copies=int(copies))
        stats.examined += 1
        if signal == SIGNAL_NOISE:
            stats.noise += 1
            DEMOTED.labels(reason="noise").inc()
        elif signal == SIGNAL_DUPLICATE:
            stats.duplicate += 1
            DEMOTED.labels(reason="duplicate").inc()
        elif signal == SIGNAL_LOW:
            stats.low += 1
            DEMOTED.labels(reason="short").inc()

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE chunks SET signal = %s, curated_at = now() WHERE id = %s",
                (signal, chunk_id),
            )

    stats.superseded = mark_superseded(conn)

    LOG.info(
        "curation pass complete",
        extra={
            "examined": stats.examined,
            "noise": stats.noise,
            "short": stats.low,
            "duplicate": stats.duplicate,
            "superseded": stats.superseded,
        },
    )
    return stats


def mark_superseded(conn: Connection) -> int:
    """Point older restatements at the newest one.

    Only identical text within one memory scope, which is the case where "this
    replaced that" is a fact rather than an inference: the same sentence stated
    again later in the same channel or on the same issue. Anything looser needs
    a model, and P3-RES-1 is where that belongs — guessing here would demote a
    correct answer in favour of a later, vaguer one, and that failure is silent.

    Scope rather than entity, because migration 006 made the entity case
    impossible: chunks are UNIQUE (entity_id, content_hash), so one entity can
    never hold the same text twice. Partitioning by entity here matched nothing
    at all, which read exactly like a corpus with no restatements in it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "WITH ranked AS ("
            "    SELECT c.id,"
            "           first_value(c.id) OVER ("
            "               PARTITION BY c.scope_id, c.content_hash"
            "               ORDER BY e.occurred_at DESC NULLS LAST,"
            "                        c.created_at DESC, c.id DESC"
            "           ) AS newest"
            "    FROM chunks c JOIN entities e ON e.id = c.entity_id"
            "    WHERE c.superseded_by IS NULL"
            ") "
            "UPDATE chunks SET superseded_by = ranked.newest "
            "FROM ranked "
            "WHERE chunks.id = ranked.id AND ranked.newest <> ranked.id"
        )
        return cur.rowcount


def recurate(conn: Connection) -> None:
    """Forget every assessment, so the next pass reassesses everything.

    What a rule change needs. Kept as its own verb rather than a flag, because
    re-scoring a corpus is a decision and should read like one at the call site.
    """
    with conn.cursor() as cur:
        cur.execute("UPDATE chunks SET curated_at = NULL, signal = 1.0, superseded_by = NULL")
    LOG.warning("curation reset; the next pass will reassess every chunk")
