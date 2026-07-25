"""Print the eval numbers.

Deliberately a CLI as well as a gate. A number that only exists inside a green
CI run is a number nobody looks at, and STACK.md expects these to be consulted
whenever someone argues for a different search index, a different embedding
model, or a different agent loop.

    python -m evals.report                      # build a scratch database
    python -m evals.report postgresql://…       # use an existing one

It builds its own corpus, because the numbers are only meaningful on a graph
large enough that each retrieval mode has to choose. The demo corpus is sixteen
chunks; measuring on it gave recall@20 = 1.000 and meant nothing.
"""

from __future__ import annotations

import sys
import uuid

from core.db import connect
from core.migrate import upgrade
from evals.harness import cases_from, run
from evals.redteam import hunt, probe_count
from evals.seed import build, fingerprint


def main(dsn: str | None) -> int:
    target = dsn or _scratch()

    with connect(target, autocommit=True) as conn:
        upgrade(conn)

    with connect(target) as conn:
        corpus = build(conn)
        conn.commit()
        cases = cases_from(corpus)
        print(
            f"corpus         {corpus.chunks} chunks, {corpus.channels} channels, "
            f"{len(corpus.facts)} planted facts  [{fingerprint(corpus)}]"
        )
        print(f"cases          {len(cases)}\n")

        for k in (3, 5, 10, 20):
            report = run(conn, cases, k=k)
            modes = report.recall_by_mode()
            print(
                f"k={k:<3} recall={report.recall_at_k:.3f} mrr={report.mrr:.3f}  "
                f"fts={modes.get('fts', 0.0):.3f} vector={modes.get('vector', 0.0):.3f} "
                f"graph={modes.get('graph', 0.0):.3f}  "
                f"lexical={report.recall_for('lexical'):.2f} "
                f"semantic={report.recall_for('semantic'):.2f} "
                f"traversal={report.recall_for('traversal'):.2f}  "
                f"leaks={len(report.leaks)}"
            )

        print("\n== red team ==")
        secrets = [fact.answer for fact in corpus.facts if fact.private]
        roster = corpus.members["C-EVAL-00"]
        outsider = next(p for p in corpus.members["C-EVAL-01"] if p not in roster)
        leaks = hunt(conn, outsider, secrets[:6])
        print(f"probes         {probe_count(secrets[:6])}")
        print(f"leaks          {len(leaks)}")
        for leak in leaks:
            print(f"  {leak.probe}: {leak.forbidden[:60]}")

    return 1 if leaks else 0


def _scratch() -> str:
    """A throwaway database, left behind on purpose: a run whose numbers looked
    wrong is a run somebody will want to poke at afterwards."""
    admin = "postgresql://localhost:5432/postgres"
    name = f"hippo_evals_{uuid.uuid4().hex[:8]}"
    with connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    print(f"(built {name}; drop it when you are done)\n")
    return f"postgresql://localhost:5432/{name}"


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
