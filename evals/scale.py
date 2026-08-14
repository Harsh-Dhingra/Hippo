"""How big a corpus this actually holds, measured rather than assumed.

Every scale claim in this repository until now has been a design argument. The
walk is bounded *by construction*, the edges are index-scanned, the grants are
per-container — all true, all reasoning, none of it a number. This produces
numbers.

    python -m evals.scale                       # the default shape, ~1M edges
    python -m evals.scale --chunks 50000        # smaller, for a laptop
    python -m evals.scale --dsn postgresql://…  # against a database you have

**It measures the pieces separately on purpose.** A single end-to-end figure
tells you the query was slow and nothing about which part. Keyword search,
vector search, the graph walk and the permission expansion fail at different
sizes for different reasons, and the one that gives out first is the only one
worth working on.

The corpus shape is chosen to resemble a company rather than to flatter the
system: most content in a few busy containers, a long tail of quiet ones, and a
small number of people who authored an unreasonable amount of it. Hubs are the
case that breaks a graph, so a benchmark without them is a benchmark that
cannot fail.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from uuid import UUID

from psycopg.conninfo import conninfo_to_dict

from core.db import Connection, connect
from core.migrate import upgrade
from evals.scratch import scratch_database

ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Roughly what a mid-size company looks like after a year on Slack and Jira.
DEFAULT_CHUNKS = 200_000
DEFAULT_EDGES = 1_000_000
DEFAULT_PEOPLE = 400
DEFAULT_CONTAINERS = 800


@dataclass
class Timing:
    """One measurement, with enough runs to mean something."""

    name: str
    runs: list[float] = field(default_factory=list)

    def _at(self, quantile: float) -> float:
        """Nearest-rank percentile.

        `int(n * q) - 1` was the first version and it discounts twice — a
        floor and then a decrement — so p95 of five samples came back as the
        fourth rather than the fifth. Ceiling then decrement is the standard
        definition and gets the one-sample case right for free.
        """
        ordered = sorted(self.runs)
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, max(0, math.ceil(quantile * len(ordered)) - 1))]

    @property
    def p50(self) -> float:
        return self._at(0.5)

    @property
    def p95(self) -> float:
        return self._at(0.95)

    def line(self) -> str:
        return f"  {self.name:<34} p50 {self.p50 * 1000:8.1f} ms   p95 {self.p95 * 1000:8.1f} ms"


@contextmanager
def timed(timing: Timing) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        timing.runs.append(time.monotonic() - started)


def build(
    conn: Connection,
    *,
    chunks: int,
    edges: int,
    people: int,
    containers: int,
) -> UUID:
    """Generate a corpus and return a principal who can see most of it.

    Set-based throughout. Generating a million edges a row at a time takes
    longer than the measurement it exists to support, and a benchmark nobody
    runs twice is a benchmark nobody trusts.
    """
    print(f"building {chunks:,} chunks, {edges:,} edges, {containers:,} containers …")
    started = time.monotonic()
    connector = uuid.uuid4()
    reader = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) "
            "VALUES (%s, 'slack', 'Scale workspace', '{}')",
            (connector,),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id) "
            "VALUES (%s, 'user', %s, 'U-READER')",
            (reader, connector),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id) "
            "SELECT gen_random_uuid(), 'user', %s, 'U-'||g FROM generate_series(1, %s) g",
            (connector, people),
        )

        # Containers first: they are what grants attach to, which is the whole
        # reason a real deployment does not have one grant per message.
        cur.execute(
            "INSERT INTO entities (id, entity_type, title, canonical_key) "
            "SELECT gen_random_uuid(), 'channel', '#c'||g, 'c'||g "
            "FROM generate_series(1, %s) g",
            (containers,),
        )
        cur.execute(
            "INSERT INTO entities (id, entity_type, title, canonical_key) "
            "SELECT gen_random_uuid(), 'person', 'P'||g, 'p'||g FROM generate_series(1, %s) g",
            (people,),
        )
        # Content. A skewed distribution over containers, because the flat one
        # has no hubs and hubs are the case that breaks a walk.
        cur.execute(
            "INSERT INTO entities (id, entity_type, title, canonical_key, occurred_at) "
            "SELECT gen_random_uuid(), 'message', 'm'||g, 'm'||g, "
            "       now() - make_interval(mins => g %% 500000) "
            "FROM generate_series(1, %s) g",
            (chunks,),
        )

        # Everyone reads every container. The widest realistic case, and the
        # one that hurts most: a reader who can see nothing is fast.
        cur.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) "
            "SELECT id, %s, 'scale' FROM entities "
            "WHERE entity_type IN ('channel','person','message')",
            (reader,),
        )

        print(f"  entities and grants  {time.monotonic() - started:5.1f}s")
        embed_started = time.monotonic()

        # One pool of vectors, perturbed per row. Generating 200 million
        # independent randoms costs minutes and buys nothing: HNSW cares about
        # the distribution, and a pool of a thousand with noise has one.
        cur.execute(
            "CREATE TEMP TABLE vector_pool AS "
            "SELECT g AS id, (SELECT array_agg(random()::real) "
            "                 FROM generate_series(1,1024))::vector(1024) AS v "
            "FROM generate_series(1, 1000) g"
        )
        cur.execute(
            "INSERT INTO chunks (entity_id, scope_id, content, chunk_index, embedding) "
            "SELECT e.id, %s, "
            "       'renewal discount cap legal review item '||e.canonical_key, 0, p.v "
            "FROM entities e "
            "JOIN vector_pool p ON p.id = (abs(hashtext(e.canonical_key)) %% 1000) + 1 "
            "WHERE e.entity_type = 'message'",
            (ORG_SCOPE,),
        )
        print(f"  chunks and vectors   {time.monotonic() - embed_started:5.1f}s")
        edge_started = time.monotonic()

        # belongs_to for every message, skewed so a handful of containers hold
        # most of it. This is the direction the walk closes, and it has to be
        # present for closing it to mean anything.
        cur.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "SELECT m.id, c.id, 'belongs_to', 'source' "
            "FROM (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='message') m "
            "JOIN (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='channel') c "
            "  ON c.n = 1 + (m.n %% %s) / 8",
            (containers * 8,),
        )
        # authored, concentrated: a few people wrote a great deal of it.
        cur.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "SELECT p.id, m.id, 'authored', 'source' "
            "FROM (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='message') m "
            "JOIN (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='person') p "
            "  ON p.n = 1 + (m.n * m.n %% %s)",
            (people,),
        )
        # replies_to, the direction the walk keeps. Threaded in runs of ten.
        cur.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "SELECT a.id, b.id, 'replies_to', 'source' "
            "FROM (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='message') a "
            "JOIN (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
            "      FROM entities WHERE entity_type='message') b "
            "  ON b.n = a.n - 1 AND a.n % 10 <> 0"
        )
        # Structural edges come out at roughly three per message. The rest of
        # the target is made up with cross-references, which is what a real
        # corpus has too: messages naming tickets, tickets naming each other.
        cur.execute("SELECT count(*) FROM edges")
        made = int((cur.fetchone() or (0,))[0])
        if edges > made:
            wanted = edges - made
            cur.execute(
                "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
                "SELECT a.id, b.id, 'references', 'source' "
                "FROM (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
                "      FROM entities WHERE entity_type='message') a "
                "CROSS JOIN generate_series(1, %s) rep "
                "JOIN (SELECT id, row_number() OVER (ORDER BY canonical_key) AS n "
                "      FROM entities WHERE entity_type='message') b "
                "  ON b.n = 1 + ((a.n * 7 + rep * 13) %% %s) "
                "WHERE a.id <> b.id "
                "ON CONFLICT DO NOTHING",
                (max(1, wanted // max(1, chunks) + 1), max(1, chunks)),
            )
            cur.execute("SELECT count(*) FROM edges")
            made = int((cur.fetchone() or (0,))[0])
        print(f"  edges ({made:,})   {time.monotonic() - edge_started:5.1f}s")

    conn.commit()
    analyse_started = time.monotonic()
    with conn.cursor() as cur:
        for table in ("entities", "edges", "chunks", "acl_grants", "principals"):
            cur.execute(f"ANALYZE {table}")
    conn.commit()
    print(f"  analyze              {time.monotonic() - analyse_started:5.1f}s")
    print(f"  total                {time.monotonic() - started:5.1f}s\n")
    return reader


def measure(conn: Connection, reader: UUID, *, runs: int = 7) -> list[Timing]:
    """Time each part of a query separately.

    Separately because they fail at different sizes for different reasons, and
    one number would say the query was slow without saying which half to fix.
    """
    questions = [
        "renewal discount cap",
        "legal review item",
        "cap legal renewal review",
        "discount item review",
    ]

    expansion = Timing("permission expansion")
    keyword = Timing("keyword only")
    vector = Timing("vector only")
    walk = Timing("graph walk, 1 hop")
    walk2 = Timing("graph walk, 2 hops")
    whole = Timing("visible_chunks, k=20 hops=1")

    with conn.cursor() as cur:
        cur.execute("SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1")
        probe = (cur.fetchone() or (None,))[0]

        for index in range(runs):
            question = questions[index % len(questions)]

            with timed(expansion):
                cur.execute("SELECT count(*) FROM _visible_entity_ids(%s)", (reader,))
                cur.fetchone()

            with timed(keyword):
                cur.execute(
                    "SELECT count(*) FROM visible_chunks(%s, %s, NULL, 20, 0)", (reader, question)
                )
                cur.fetchone()

            with timed(vector):
                cur.execute(
                    "SELECT count(*) FROM visible_chunks(%s, NULL, %s, 20, 0)", (reader, probe)
                )
                cur.fetchone()

            with timed(walk):
                cur.execute(
                    "SELECT count(*) FROM visible_chunks(%s, %s, NULL, 20, 1)", (reader, question)
                )
                cur.fetchone()

            with timed(walk2):
                cur.execute(
                    "SELECT count(*) FROM visible_chunks(%s, %s, NULL, 20, 2)", (reader, question)
                )
                cur.fetchone()

            with timed(whole):
                cur.execute(
                    "SELECT count(*) FROM visible_chunks(%s, %s, %s, 20, 1)",
                    (reader, question, probe),
                )
                cur.fetchone()

    return [expansion, keyword, vector, walk, walk2, whole]


def sizes(conn: Connection) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT (SELECT count(*) FROM entities), (SELECT count(*) FROM edges), "
            "       (SELECT count(*) FROM chunks), (SELECT count(*) FROM acl_grants), "
            "       pg_size_pretty(pg_database_size(current_database()))"
        )
        row = cur.fetchone()
    assert row is not None
    return (
        f"  {row[0]:,} entities   {row[1]:,} edges   {row[2]:,} chunks   "
        f"{row[3]:,} grants   {row[4]} on disk"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.scale", description=__doc__)
    parser.add_argument("--chunks", type=int, default=DEFAULT_CHUNKS)
    parser.add_argument("--edges", type=int, default=DEFAULT_EDGES)
    parser.add_argument("--people", type=int, default=DEFAULT_PEOPLE)
    parser.add_argument("--containers", type=int, default=DEFAULT_CONTAINERS)
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--dsn", default=None)
    args = parser.parse_args(argv)

    target = args.dsn
    if target is None:
        # Not `createdb`: that is a binary which may not be installed, and it
        # takes no credentials, so the no-argument path only ever worked on a
        # machine with a trusted local socket.
        target = scratch_database("hippo_scale")
        print(f"(built {conninfo_to_dict(target)['dbname']}; drop it when you are done)\n")

    with connect(target, autocommit=True) as conn:
        upgrade(conn)

    with connect(target) as conn:
        reader = build(
            conn,
            chunks=args.chunks,
            edges=args.edges,
            people=args.people,
            containers=args.containers,
        )
        print(sizes(conn) + "\n")
        for timing in measure(conn, reader, runs=args.runs):
            print(timing.line())

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
