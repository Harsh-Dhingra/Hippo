"""P2-EVAL-2: running somebody else's benchmark.

The point of these tests is not that the loader works. It is that the runner
keeps one distinction the published benchmarks do not make: a benchmark with no
permission model cannot report zero leaks, because it has no leak to find. If
that ever collapses into a plain `leaks: 0`, the report starts claiming a
result it did not measure.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core.db import Connection
from evals.external import Benchmark, load_into, run
from resolver.embeddings import HashingEmbeddings

pytestmark = pytest.mark.requires_db


PERMISSIONED: dict[str, Any] = {
    "name": "with-acls",
    "source": "https://example.invalid/benchmark",
    "people": [
        {"id": "alice", "email": "alice@example.com", "groups": ["legal"]},
        {"id": "bob", "email": "bob@example.com", "groups": ["sales"]},
    ],
    "documents": [
        {
            "id": "legal-memo",
            "title": "Renewal",
            "text": "The liability cap is the blocker on the Acme renewal and legal own it",
            "readable_by": ["legal"],
        },
        {
            "id": "sales-note",
            "title": "Pipeline",
            "text": "Acme renewal is forecast to close next quarter at the standard discount",
            "readable_by": ["sales"],
        },
    ],
    "questions": [
        {
            "id": "q-legal",
            "asker": "alice",
            "question": "What is the blocker on the Acme renewal?",
            "answer_in": ["legal-memo"],
            "must_not_see": ["sales-note"],
        },
        {
            "id": "q-sales",
            "asker": "bob",
            "question": "What is the blocker on the Acme renewal?",
            "answer_in": ["sales-note"],
            "must_not_see": ["legal-memo"],
        },
    ],
}

OPEN: dict[str, Any] = {
    "name": "no-acls",
    "documents": [
        {"id": "d1", "text": "The liability cap is the blocker on the Acme renewal"},
        {"id": "d2", "text": "Unrelated content about the office move in March"},
    ],
    "questions": [
        {"id": "q1", "asker": "someone", "question": "Acme renewal blocker", "answer_in": ["d1"]}
    ],
}


def loaded(conn: Connection, payload: dict[str, Any]) -> Benchmark:
    benchmark = Benchmark.model_validate(payload)
    load_into(conn, benchmark, HashingEmbeddings())
    return benchmark


def test_a_benchmark_with_acls_measures_them(migrated: Connection) -> None:
    """Two people, the same question, different entitlements. This is the shape
    that can actually test the claim."""
    benchmark = loaded(migrated, PERMISSIONED)

    result = run(migrated, benchmark)

    assert result.measured_permissions is True
    assert result.leaks == ()
    assert result.recall_at_k == 1.0


def test_each_person_gets_only_their_own_documents(migrated: Connection) -> None:
    benchmark = loaded(migrated, PERMISSIONED)

    result = run(migrated, benchmark)

    by_id = {r.id: r for r in result.results}
    assert by_id["q-legal"].retrieved == ("legal-memo",)
    assert by_id["q-sales"].retrieved == ("sales-note",)


def test_a_benchmark_without_acls_reports_that_it_measured_nothing(
    migrated: Connection,
) -> None:
    """The distinction the whole module exists to keep. Zero out of zero
    possible leaks is not a result, and printing "leaks 0" would claim one."""
    benchmark = loaded(migrated, OPEN)

    result = run(migrated, benchmark)

    assert result.measured_permissions is False
    assert "not measured" in result.summary()
    assert "leaks          0" not in result.summary()


def test_a_permissioned_benchmark_says_so_in_its_summary(migrated: Connection) -> None:
    benchmark = loaded(migrated, PERMISSIONED)

    summary = run(migrated, benchmark).summary()

    assert "leaks          0" in summary
    assert "not measured" not in summary


def test_a_leak_is_reported_rather_than_averaged(migrated: Connection) -> None:
    """Leaks are a bug list, not a metric. One is a release blocker, so it is
    named rather than folded into a rate."""
    from evals.external import QuestionResult, Result

    result = Result(
        name="x",
        k=20,
        measured_permissions=True,
        results=(
            QuestionResult(id="q1", retrieved=("secret",), expected=(), forbidden_seen=("secret",)),
        ),
    )

    assert result.leaks == ("q1: secret",)
    assert "q1: secret" in result.summary()


def test_loading_is_deterministic(migrated: Connection) -> None:
    """Ids come from the benchmark's own ids, so two runs of one file produce
    the same rows and a diff between runs is a real change."""
    loaded(migrated, PERMISSIONED)
    with migrated.cursor() as cur:
        cur.execute("SELECT id FROM entities ORDER BY id")
        first = cur.fetchall()

    loaded(migrated, PERMISSIONED)
    with migrated.cursor() as cur:
        cur.execute("SELECT id FROM entities ORDER BY id")
        assert cur.fetchall() == first


def test_content_goes_through_the_ordinary_filter(migrated: Connection) -> None:
    """No side door. A benchmark loaded through a special path would measure a
    system nobody uses."""
    benchmark = loaded(migrated, PERMISSIONED)
    result = run(migrated, benchmark)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_grants WHERE source = 'benchmark'")
        grants = (cur.fetchone() or (0,))[0]

    assert grants == 2, "audiences became ordinary grants"
    assert result.results


def test_the_format_round_trips_through_json(tmp_path: Path) -> None:
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(PERMISSIONED))

    benchmark = Benchmark.load(path)

    assert benchmark.name == "with-acls"
    assert benchmark.has_permission_model is True
    assert len(benchmark.questions) == 2


def test_an_empty_audience_is_not_the_same_as_no_audience() -> None:
    """Absent means "this benchmark has no permission model". Empty means
    "nobody may read this", which is a real state worth being able to express.
    """
    absent = Benchmark.model_validate({"name": "a", "documents": [{"id": "d", "text": "x"}]})
    empty = Benchmark.model_validate(
        {"name": "b", "documents": [{"id": "d", "text": "x", "readable_by": []}]}
    )

    assert absent.has_permission_model is False
    assert empty.has_permission_model is True


def test_a_document_nobody_may_read_is_returned_to_nobody(migrated: Connection) -> None:
    benchmark = loaded(
        migrated,
        {
            "name": "sealed",
            "people": [{"id": "alice"}],
            "documents": [
                {"id": "sealed", "text": "the liability cap on the Acme renewal", "readable_by": []}
            ],
            "questions": [
                {
                    "id": "q1",
                    "asker": "alice",
                    "question": "Acme renewal liability cap",
                    "must_not_see": ["sealed"],
                }
            ],
        },
    )

    result = run(migrated, benchmark)

    assert result.results[0].retrieved == ()
    assert result.leaks == ()


# ---------------------------------------------------------------------------
# The CLI, which is how this will actually be used.
# ---------------------------------------------------------------------------


def test_the_cli_runs_a_benchmark_end_to_end(
    db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Including the migration, because someone running a published benchmark
    should not have to set a database up first."""
    from evals.external import main

    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(PERMISSIONED))

    assert main([str(path), db_dsn]) == 0

    printed = capsys.readouterr().out
    assert "benchmark      with-acls" in printed
    assert "leaks          0" in printed
    assert "https://example.invalid/benchmark" in printed


def test_the_cli_exits_non_zero_on_a_leak(
    db_dsn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """So it can be a CI gate rather than something somebody reads."""
    from evals.external import main

    leaky: dict[str, Any] = {
        "name": "leaky",
        "people": [{"id": "alice"}],
        "documents": [
            {
                "id": "open-secret",
                "text": "the liability cap on the Acme renewal is the blocker",
                "readable_by": ["alice"],
            }
        ],
        # Readable by this asker, and the benchmark says it should not come
        # back: a labelling error in the benchmark rather than a leak in Hippo.
        # It still has to be reported, because the runner cannot tell which.
        "questions": [
            {
                "id": "q1",
                "asker": "alice",
                "question": "Acme renewal liability cap blocker",
                "must_not_see": ["open-secret"],
            }
        ],
    }
    path = tmp_path / "leaky.json"
    path.write_text(json.dumps(leaky))

    assert main([str(path), db_dsn]) == 1
    assert "q1: open-secret" in capsys.readouterr().out


def test_the_cli_with_no_arguments_explains_itself(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from evals.external import main

    assert main([]) == 2
    assert "readable_by" in capsys.readouterr().out


def test_a_question_with_no_expected_documents_does_not_skew_recall(
    migrated: Connection,
) -> None:
    """ "I don't have anything on that" is a legitimate expected answer, and
    averaging a zero into recall for it would punish the right behaviour."""
    benchmark = loaded(
        migrated,
        {
            "name": "unanswerable",
            "people": [{"id": "alice"}],
            "documents": [{"id": "d1", "text": "the office move is in March", "readable_by": []}],
            "questions": [
                {"id": "q1", "asker": "alice", "question": "what is our revenue"},
            ],
        },
    )

    result = run(migrated, benchmark)

    assert result.recall_at_k == 0.0
    assert result.mrr == 0.0
    assert result.results[0].recall == 0.0


def test_the_rank_of_the_first_correct_document_is_reported(migrated: Connection) -> None:
    benchmark = loaded(migrated, PERMISSIONED)

    result = run(migrated, benchmark)

    assert all(r.first_rank == 1 for r in result.results)
