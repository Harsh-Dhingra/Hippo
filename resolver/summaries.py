"""Entity summaries.

Regenerated, never appended (ARCHITECTURE section 6). A summary is derived
data: it is recomputed from the chunks that exist now, and the previous one is
overwritten rather than added to. That is what stops stale summaries
accumulating, and it is why re-running enrichment is safe.

Two implementations, and the honest position on the second one:

**ExtractiveSummarizer** is deterministic and calls nothing. It takes the
opening of each chunk up to a budget. It is a table of contents, not a
synthesis, and it is what CI runs and what the project does before a model is
configured.

**A model-backed summarizer** is the intended default and is not here yet. It
needs the provider interface from P1-AGT-1, which is the next fragment on the
critical path. The protocol below is the seam it plugs into, so enrichment does
not change when it arrives.

ARCHITECTURE calls enrichment the only stage that talks to a model. Today, with
the extractive summarizer, it talks to nothing at all.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Protocol

# Long enough to be worth reading in a citation list, short enough that a
# hundred of them still fit in a prompt.
SUMMARY_BUDGET = 400

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class Summarizer(Protocol):
    """Chunks in, one summary out."""

    name: str

    def summarize(self, title: str | None, chunks: Sequence[str]) -> str | None: ...


class ExtractiveSummarizer:
    """Deterministic. The opening sentence of each chunk, up to a budget."""

    def __init__(self, budget: int = SUMMARY_BUDGET) -> None:
        self.name = "extractive"
        self._budget = budget

    def summarize(self, title: str | None, chunks: Sequence[str]) -> str | None:
        pieces: list[str] = []
        used = 0
        for chunk in chunks:
            sentence = _first_sentence(chunk)
            if not sentence or sentence in pieces:
                continue
            if used + len(sentence) > self._budget and pieces:
                break
            pieces.append(sentence)
            used += len(sentence)

        if not pieces:
            # Nothing worth summarising. Returning the title would make the
            # summary a duplicate of a column that already exists.
            return None
        return " ".join(pieces)[: self._budget]


def _first_sentence(text: str) -> str:
    flattened = " ".join(text.split())
    if not flattened:
        return ""
    return _SENTENCE_END.split(flattened, maxsplit=1)[0].strip()
