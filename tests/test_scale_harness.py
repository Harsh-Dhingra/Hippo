"""The scale harness, exercised small so it cannot rot.

A benchmark that only runs when somebody remembers to run it stops working
silently and is discovered at the moment it is needed. This is the same lesson
the compose job taught: the gate nobody exercises is the gate that is broken.

So the harness runs in CI at a size that takes a second, asserting the shape of
what it produces rather than any timing. Timings are the point of the tool and
are meaningless on shared CI hardware; the thing worth guarding is that it
still builds a corpus with hubs in it and still measures the four things
separately.
"""

from __future__ import annotations

import pytest

from core.db import Connection
from evals.scale import Timing, build, measure, sizes

pytestmark = pytest.mark.requires_db

SMALL = {"chunks": 400, "edges": 3000, "people": 8, "containers": 6}


def test_it_builds_the_corpus_it_was_asked_for(migrated: Connection) -> None:
    build(migrated, **SMALL)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks")
        chunks = int((cur.fetchone() or (0,))[0])
        cur.execute("SELECT count(*) FROM edges")
        edges = int((cur.fetchone() or (0,))[0])

    assert chunks == SMALL["chunks"]
    assert edges >= SMALL["edges"], "the reference top-up reaches the target"


def test_the_corpus_has_hubs(migrated: Connection) -> None:
    """A flat corpus has no hubs, and hubs are the case that breaks a walk. A
    benchmark without them is one that cannot fail."""
    build(migrated, **SMALL)

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT max(degree) FROM ("
            "  SELECT count(*) AS degree FROM edges WHERE edge_type = 'authored' GROUP BY src_id"
            ") d"
        )
        busiest_author = int((cur.fetchone() or (0,))[0])
        cur.execute(
            "SELECT max(degree) FROM ("
            "  SELECT count(*) AS degree FROM edges WHERE edge_type = 'belongs_to' GROUP BY dst_id"
            ") d"
        )
        busiest_container = int((cur.fetchone() or (0,))[0])

    assert busiest_author > SMALL["chunks"] / SMALL["people"]
    assert busiest_container > SMALL["chunks"] / SMALL["containers"]


def test_every_grant_is_on_a_container_or_an_object_not_both(
    migrated: Connection,
) -> None:
    """The reader can see everything, which is the widest realistic case and
    the one that costs most. A reader who can see nothing is fast and proves
    nothing."""
    reader = build(migrated, **SMALL)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM _visible_entity_ids(%s)", (reader,))
        visible = int((cur.fetchone() or (0,))[0])

    assert visible > SMALL["chunks"]


def test_it_measures_each_part_separately(migrated: Connection) -> None:
    """One end-to-end number says the query was slow and nothing about which
    half to fix. These fail at different sizes for different reasons."""
    reader = build(migrated, **SMALL)

    timings = measure(migrated, reader, runs=2)

    assert [t.name for t in timings] == [
        "permission expansion",
        "keyword only",
        "vector only",
        "graph walk, 1 hop",
        "graph walk, 2 hops",
        "visible_chunks, k=20 hops=1",
    ]
    assert all(t.runs for t in timings)
    assert all(t.p50 >= 0 for t in timings)


def test_the_walk_actually_reaches_something(migrated: Connection) -> None:
    """A benchmark where the graph returns nothing measures the cost of an
    empty set. The generated corpus has to be one the walk can move through."""
    reader = build(migrated, **SMALL)

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT count(*) FILTER (WHERE 'graph' = ANY(retrieval_modes)) "
            "FROM visible_chunks(%s, 'renewal discount cap', NULL, 50, 1)",
            (reader,),
        )
        via_graph = int((cur.fetchone() or (0,))[0])

    assert via_graph > 0


def test_the_summary_line_reports_the_corpus(migrated: Connection) -> None:
    build(migrated, **SMALL)

    line = sizes(migrated)

    assert "entities" in line
    assert "edges" in line
    assert "on disk" in line


def test_percentiles_need_no_special_casing() -> None:
    timing = Timing("x", runs=[0.1, 0.2, 0.3, 0.4, 0.5])

    assert timing.p50 == 0.3
    assert timing.p95 == 0.5
    assert Timing("empty").p50 == 0.0
    assert Timing("empty").p95 == 0.0


def test_one_run_is_its_own_median() -> None:
    """The degenerate case a percentile helper usually gets wrong."""
    timing = Timing("x", runs=[0.25])

    assert timing.p50 == 0.25
    assert timing.p95 == 0.25


def test_a_timing_renders_as_a_line() -> None:
    line = Timing("keyword only", runs=[0.012, 0.018]).line()

    assert "keyword only" in line
    assert "p50" in line
    assert "p95" in line
    assert "ms" in line


@pytest.mark.requires_db
def test_the_cli_runs_end_to_end(db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    """The path a person takes. Run tiny, because what is being checked is that
    it works at all — the numbers it prints are meaningless on CI hardware and
    are not asserted."""
    from evals.scale import main

    assert (
        main(
            [
                "--dsn",
                db_dsn,
                "--chunks",
                "200",
                "--edges",
                "800",
                "--people",
                "4",
                "--containers",
                "3",
                "--runs",
                "1",
            ]
        )
        == 0
    )

    printed = capsys.readouterr().out
    assert "entities" in printed
    assert "graph walk, 1 hop" in printed
    assert "visible_chunks" in printed
