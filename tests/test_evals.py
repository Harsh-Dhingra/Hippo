"""P2-EVAL-1's done-condition: the eval harness, as a gate that runs forever.

Two suites with different characters, and the difference matters.

**The golden set** is a quality bar. Its floors are set below the numbers
measured when it was written, and they ratchet upward the way coverage does. A
number falling below one is a regression to explain, not a build to panic
about.

**The red team** is not a bar. Leaks are zero, one is a release-blocking bug,
and there is no threshold to negotiate.

The corpus is generated once per module. Building 432 chunks with embeddings
costs a couple of seconds, and paying that per test would make the suite
something people skip.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from core.db import Connection, connect
from core.migrate import upgrade
from evals.golden import GOLDEN
from evals.harness import cases_from, run
from evals.redteam import hunt, probe_count, probes_for
from evals.seed import Corpus, build, fingerprint

pytestmark = [pytest.mark.requires_db, pytest.mark.eval]

# Measured on 2026-07-25 against migration 014: recall 0.756, MRR 0.421,
# lexical 1.00, traversal 0.75, semantic 0.33. The floors sit below those, so a
# real regression fails and nothing else does. Raise them when retrieval
# improves; never lower one to make a change pass.
#
# The corpus is fully deterministic — derived ids, fixed seed — so these numbers
# are the same on every machine. That is what makes them safe to gate on: the
# filter breaks score ties by chunk id, and random ids moved the result by
# several points between runs.
FLOOR_RECALL_AT_20 = 0.70
FLOOR_MRR = 0.35
FLOOR_LEXICAL = 0.95
FLOOR_TRAVERSAL = 0.65
# The lexical embedder is genuinely poor at paraphrase, and pretending
# otherwise with a high floor would make this number meaningless. It is here to
# be watched when a real embedding model is chosen at P1-RES-3, and to be
# raised sharply when one is.
FLOOR_SEMANTIC = 0.25


@pytest.fixture(scope="module")
def seeded(request: pytest.FixtureRequest) -> Iterator[tuple[Connection, Corpus]]:
    """A purpose-built graph, generated once."""
    dsn = _module_database(request)
    with connect(dsn, autocommit=True) as conn:
        upgrade(conn)
    with connect(dsn) as conn:
        corpus = build(conn)
        conn.commit()
        yield conn, corpus


def _module_database(request: pytest.FixtureRequest) -> str:
    """A throwaway database for this module, torn down with it."""
    import uuid

    admin = str(request.getfixturevalue("admin_dsn"))
    name = f"hippo_eval_{uuid.uuid4().hex[:12]}"
    with connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')

    def drop() -> None:
        with connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    request.addfinalizer(drop)
    return admin.rsplit("/", 1)[0] + "/" + name


# ---------------------------------------------------------------------------
# The corpus is what it claims to be.
# ---------------------------------------------------------------------------


def test_the_corpus_is_big_enough_to_measure_on(seeded: tuple[Connection, Corpus]) -> None:
    """The reason this exists. The fixture corpus is sixteen chunks, so a k of
    20 returns everything and recall is true by arithmetic — which is how the
    first version of this suite scored 1.000 and measured nothing."""
    _, corpus = seeded

    assert corpus.chunks > 400
    # The vector mode's internal limit is k * 4. Well under the corpus, so the
    # mode has to choose.
    assert corpus.chunks > 20 * 4 * 2


def test_the_corpus_is_deterministic(seeded: tuple[Connection, Corpus]) -> None:
    """Numbers that move between runs are numbers nobody gates on."""
    _, corpus = seeded

    assert fingerprint(corpus) == "bb4b7ee414dfe915"


def test_every_mode_has_something_only_it_can_find(
    seeded: tuple[Connection, Corpus],
) -> None:
    _, corpus = seeded
    kinds = {fact.kind for fact in corpus.facts}

    assert kinds == {"lexical", "semantic", "traversal"}


def test_some_of_it_is_private(seeded: tuple[Connection, Corpus]) -> None:
    """Without this the red team has nothing to fail to reach."""
    _, corpus = seeded

    assert any(fact.private for fact in corpus.facts)
    assert any(not fact.private for fact in corpus.facts)


# ---------------------------------------------------------------------------
# The golden set, as floors.
# ---------------------------------------------------------------------------


def test_recall_holds_at_twenty(seeded: tuple[Connection, Corpus]) -> None:
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.recall_at_k >= FLOOR_RECALL_AT_20, report.summary()


def test_the_answer_ranks_near_the_top(seeded: tuple[Connection, Corpus]) -> None:
    """Recall says the answer was in the set. MRR says it was near the top,
    which is what decides whether it survives a context budget."""
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.mrr >= FLOOR_MRR, report.summary()


def test_keyword_retrieval_finds_the_rare_tokens(seeded: tuple[Connection, Corpus]) -> None:
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.recall_for("lexical") >= FLOOR_LEXICAL, report.summary()


def test_traversal_reaches_what_only_an_edge_connects(
    seeded: tuple[Connection, Corpus],
) -> None:
    """This was 0.00 before migration 014. Graph expansion reached the right
    chunk and scored it identically to fifty irrelevant siblings, so it never
    survived k. A floor here is what stops that recurring."""
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.recall_for("traversal") >= FLOOR_TRAVERSAL, report.summary()


def test_semantic_retrieval_is_measured_even_though_it_is_weak(
    seeded: tuple[Connection, Corpus],
) -> None:
    """The offline embedder matches on shared words, and the semantic cases
    share none with their questions by construction. The floor is low on
    purpose and the number is the point: it is the baseline a real embedding
    model has to beat at P1-RES-3."""
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.recall_for("semantic") >= FLOOR_SEMANTIC, report.summary()


def test_hybrid_beats_either_mode_alone(seeded: tuple[Connection, Corpus]) -> None:
    """The measured case for fusing at all. STACK.md's graduation triggers are
    written against exactly this comparison: if keyword search alone ever
    matched the fused result, the argument for the other modes would be over."""
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)
    modes = report.recall_by_mode()

    assert report.recall_at_k > modes.get("fts", 0.0), report.summary()
    assert report.recall_at_k > modes.get("vector", 0.0), report.summary()


def test_the_report_reads_as_something_a_person_can_act_on(
    seeded: tuple[Connection, Corpus],
) -> None:
    conn, corpus = seeded

    summary = run(conn, cases_from(corpus), k=20).summary()

    for expected in ("recall@k", "MRR", "leaks", "by fts", "lexical", "traversal"):
        assert expected in summary


# ---------------------------------------------------------------------------
# The red team. Not a floor.
# ---------------------------------------------------------------------------


def test_no_golden_case_leaks(seeded: tuple[Connection, Corpus]) -> None:
    """Half the derived cases ask a private question as someone outside the
    channel. Every one must come back with nothing."""
    conn, corpus = seeded

    report = run(conn, cases_from(corpus), k=20)

    assert report.leaks == (), report.summary()


def test_nothing_private_survives_a_hunt(seeded: tuple[Connection, Corpus]) -> None:
    """Every probe shape, against every private fact, as someone who cannot see
    it. The probes are built from the forbidden text itself: an attacker who
    has seen a message elsewhere is quoting, not guessing."""
    conn, corpus = seeded

    outsiders_secrets: dict[str, list[str]] = {}
    for fact in corpus.facts:
        if not fact.private:
            continue
        roster = corpus.members[fact.channel]
        outsider = next(
            (person for person in corpus.members["C-EVAL-01"] if person not in roster), None
        )
        if outsider is not None:
            outsiders_secrets.setdefault(outsider, []).append(fact.answer)

    assert outsiders_secrets, "no outsider/secret pairs; the corpus is not adversarial"

    for asker, secrets in outsiders_secrets.items():
        leaks = hunt(conn, asker, secrets)
        assert leaks == [], [leak.model_dump() for leak in leaks]


def test_the_hunt_actually_probes(seeded: tuple[Connection, Corpus]) -> None:
    """A suite that quietly stopped probing would pass. The count is asserted
    so that shows up as a number falling rather than as a green tick."""
    _, corpus = seeded
    secrets = [fact.answer for fact in corpus.facts if fact.private][:3]

    assert probe_count(secrets) >= 30
    assert len(list(probes_for(secrets[0]))) >= 8


def test_browse_mode_returns_only_what_you_can_see(
    seeded: tuple[Connection, Corpus],
) -> None:
    """The bluntest attack: ask for everything. A filter bug shows up here as a
    count rather than as a match."""
    conn, corpus = seeded
    from agent.retrieval import RetrievalPlan, retrieve
    from evals.harness import principal_id
    from resolver.embeddings import HashingEmbeddings

    private_texts = {fact.answer for fact in corpus.facts if fact.private}
    roster = corpus.members["C-EVAL-00"]
    outsider = next(person for person in corpus.members["C-EVAL-01"] if person not in roster)

    hits = retrieve(
        conn,
        principal_id(conn, outsider),
        RetrievalPlan(query_text="", k=100, hops=2),
        HashingEmbeddings(),
    )

    reachable = {hit.content for hit in hits}
    forbidden = {
        fact.answer
        for fact in corpus.facts
        if fact.private and outsider not in corpus.members[fact.channel]
    }
    assert reachable & forbidden == set()
    assert private_texts, "the corpus has private facts to be excluded from"


# ---------------------------------------------------------------------------
# The demo corpus still passes its own labelled set.
# ---------------------------------------------------------------------------


def test_the_hand_labelled_set_is_still_coherent() -> None:
    """The fixture-corpus golden set is too small to gate on, but its labels
    are still the clearest statement of what the demo should do."""
    assert len(GOLDEN) >= 14
    assert any(case.must_not_retrieve for case in GOLDEN)
    assert all(case.tests for case in GOLDEN)


# ---------------------------------------------------------------------------
# The harness itself. A gate nobody has tested is a gate nobody should trust.
# ---------------------------------------------------------------------------


def test_an_empty_report_scores_zero_rather_than_dividing_by_zero() -> None:
    from evals.harness import Report

    empty = Report(k=20, results=())

    assert empty.recall_at_k == 0.0
    assert empty.mrr == 0.0
    assert empty.recall_by_mode() == {}
    assert empty.recall_for("lexical") == 0.0
    assert empty.leaks == ()


def test_a_case_that_expects_nothing_counts_as_satisfied() -> None:
    """Three quarters of the derived cases are someone asking a question they
    are not entitled to an answer to. Scoring those zero would drag the
    headline number down for behaving correctly."""
    from evals.harness import CaseResult, Report

    denied = CaseResult(case_id="x-denied", asker="U", retrieved=0, expected=(), leaked=())

    assert denied.recall == 1.0
    assert denied.first_rank is None
    assert Report(k=20, results=(denied,)).mrr == 0.0, "and contributes no rank"


def test_a_missed_expectation_scores_zero() -> None:
    from evals.harness import CaseResult, Found

    missed = CaseResult(
        case_id="x",
        asker="U",
        retrieved=5,
        expected=(Found(needle="n", rank=None),),
        leaked=(),
    )

    assert missed.recall == 0.0
    assert missed.first_rank is None


def test_the_summary_names_what_was_missed_and_leaked() -> None:
    """A report that only gave totals would make a person re-run it with
    different code to find out which case moved."""
    from evals.harness import CaseResult, Found, Report

    report = Report(
        k=20,
        results=(
            CaseResult(
                case_id="lexical-9",
                asker="U",
                retrieved=3,
                expected=(Found(needle="wanted", rank=None),),
                leaked=("forbidden",),
            ),
        ),
    )

    summary = report.summary()
    assert "lexical-9" in summary
    assert "wanted" in summary
    assert "forbidden" in summary
    assert report.leaks == ("lexical-9 (U): forbidden",)


def test_an_unknown_principal_is_a_loud_failure(seeded: tuple[Connection, Corpus]) -> None:
    """Silently returning nothing would look exactly like a perfect filter."""
    conn, _ = seeded
    from evals.harness import principal_id

    with pytest.raises(LookupError, match="seeded"):
        principal_id(conn, "U-NOBODY")


def test_a_short_secret_still_gets_probed() -> None:
    """Shuffling needs more than two distinctive words, so a short secret takes
    a different path through the generator. It must still be probed, and the
    reported count must still match what is actually run."""
    short = list(probes_for("delivery halted"))
    long = list(probes_for("the delivery was brought to a standstill"))

    assert len(short) >= 5
    assert len(long) > len(short)
    assert any(probe.label == "shuffled" for probe in long)
    assert not any(probe.label == "shuffled" for probe in short)
    assert probe_count(["delivery halted"]) == len(short) + 10


def test_seeding_twice_fails_loudly(seeded: tuple[Connection, Corpus]) -> None:
    """Ids are derived, so a second build collides instead of silently doubling
    the corpus. Quietly doubling it would change every number in the report
    without changing anything about retrieval."""
    from psycopg import errors

    conn, _ = seeded

    with pytest.raises(errors.UniqueViolation):
        build(conn)
    conn.rollback()


def test_the_report_cli_runs_and_says_zero_leaks(
    request: pytest.FixtureRequest, capsys: pytest.CaptureFixture[str]
) -> None:
    """The thing a person actually runs. STACK.md expects these numbers to be
    consulted when someone argues for a different index, so the CLI has to work
    and not only the library behind it.

    Its own database, because the CLI seeds one — running it against a corpus
    that already exists is the collision above.
    """
    from evals.report import main

    assert main(_module_database(request)) == 0

    printed = capsys.readouterr().out
    assert "corpus" in printed
    assert "432 chunks" in printed
    assert "k=20" in printed
    assert "leaks          0" in printed


def test_the_cli_can_make_its_own_database() -> None:
    """The no-argument path, so a first-time reader gets numbers without
    setting anything up first."""
    from evals.report import _scratch

    dsn = _scratch()
    try:
        assert dsn.startswith("postgresql://")
        with connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone() == (1,)
    finally:
        name = dsn.rsplit("/", 1)[1]
        with connect("postgresql://localhost:5432/postgres", autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
