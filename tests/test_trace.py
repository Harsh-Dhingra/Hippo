"""P1-AGT-4's done-condition: every step of the §12 demo visible in the trace.

The three demo steps get one test class each, asserting on what a person
looking at the trace view would actually be able to tell. The interesting one
is the second: the trace of a filtered query has to show that nothing was
hidden *from the answer* — the retrieval list is short because the filter
returned little, not because something was stripped afterwards.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from agent.links import load_directory
from agent.loop import Agent, render_sources
from agent.providers.base import (
    Completion,
    CompletionRequest,
    PermanentProviderError,
    Usage,
)
from agent.trace import Trace, content_hash, list_traces, load_trace, record
from core.db import Connection
from resolver.embeddings import HashingEmbeddings
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime, project_acl_grants
from tests.pipeline import principal, resolve_and_enrich

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PRIVATE_TEXT = "Acme is asking for 30 percent off to renew, do not repeat outside this channel"
QUESTION = "What is blocking the Acme renewal?"


class ScriptedModel:
    name = "scripted"
    model = "scripted-1"

    def __init__(self, reply: str = "The blocker is legal review. [1]", fail: bool = False) -> None:
        self.reply = reply
        self.fail = fail
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        if self.fail:
            raise PermanentProviderError("unknown model")
        return Completion(
            text=self.reply,
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=321, output_tokens=45),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@pytest.fixture
def workspace(migrated: Connection) -> tuple[UUID, UUID]:
    slack_id, jira_id = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) VALUES "
            "(%s, 'slack', 'Slack', '{\"workspace_url\": \"https://acme.slack.com\"}'), "
            "(%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
            (slack_id, jira_id),
        )
    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(migrated)
    SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(migrated)
    resolve_and_enrich(migrated)
    project_acl_grants(migrated, slack_id)
    project_acl_grants(migrated, jira_id)
    return slack_id, jira_id


def build_agent(conn: Connection, model: Any) -> Agent:
    return Agent(model, embedder=HashingEmbeddings(), directory=load_directory(conn))


def step_names(trace: dict[str, Any]) -> list[str]:
    return [step["name"] for step in trace["steps"]]


# ---------------------------------------------------------------------------
# §12 point 1: the cited answer.
# ---------------------------------------------------------------------------


def test_every_graph_step_appears_in_the_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)

    assert answer.trace_id is not None
    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert step_names(trace) == ["plan", "retrieve", "synthesize"]


def test_the_trace_says_why_the_query_searched_the_way_it_did(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """Half a trace is what came back. The other half is why it was asked
    for."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert "keyword" in trace["plan"]["rationale"]
    assert trace["plan"]["hops"] == 2, "a blocking question walks the graph"


def test_the_trace_lists_every_retrieved_chunk_in_rank_order(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    retrievals = trace["retrievals"]
    assert len(retrievals) == len(answer.hits)
    assert [item["rank"] for item in retrievals] == list(range(1, len(answer.hits) + 1))
    assert [UUID(item["chunk_id"]) for item in retrievals] == [h.chunk_id for h in answer.hits]


def test_the_trace_records_which_mode_found_each_chunk(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """Hybrid retrieval is only worth its complexity if you can see it
    working."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    modes: set[str] = set()
    for item in trace["retrievals"]:
        modes.update(item["retrieval_modes"])
    assert {"fts", "vector"} <= modes


def test_the_trace_marks_which_chunks_the_answer_used(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """Retrieved and cited are different things, and the gap between them is
    what tells you whether an answer is grounded."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    cited = {UUID(item["entity_id"]) for item in trace["retrievals"] if item["cited"]}
    assert cited == set(answer.cited_entity_ids)
    assert len(cited) < len(trace["retrievals"]), "not everything retrieved was used"


def test_the_trace_records_the_model_and_the_token_cost(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert trace["model"] == "scripted-1"
    assert trace["provider"] == "scripted"
    assert trace["input_tokens"] == 321
    assert trace["output_tokens"] == 45


def test_the_trace_stores_the_answer_and_its_citations(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert trace["answer"] == answer.text
    assert [UUID(str(entity)) for entity in trace["citations"]] == list(answer.cited_entity_ids)


# ---------------------------------------------------------------------------
# The prompt is reconstructible, and the reconstruction is checkable.
# ---------------------------------------------------------------------------


def test_the_system_prompt_is_stored_verbatim(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """ARCHITECTURE §9: the only egress is the prompt, and that claim is worth
    nothing unless someone can check what was in it."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    model = ScriptedModel()
    answer = build_agent(migrated, model).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert trace["system_prompt"] == model.requests[-1].system


def test_the_prompt_can_be_rebuilt_from_the_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """The whole point of storing hashes instead of content: rendering the same
    chunks through the same function reproduces the prompt exactly."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    model = ScriptedModel()
    answer = build_agent(migrated, model).answer(migrated, alice, QUESTION, k=40)

    rebuilt = render_sources(list(answer.hits))

    assert rebuilt in model.requests[-1].messages[0].content


def test_a_changed_chunk_no_longer_matches_its_recorded_hash(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """A stored copy would have hidden this. The hash makes 'the source has
    changed since' visible instead of silently wrong."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None
    trace = load_trace(migrated, alice, answer.trace_id)
    assert trace is not None
    first = trace["retrievals"][0]

    with migrated.cursor() as cur:
        cur.execute(
            "UPDATE chunks SET content = %s WHERE id = %s", ("edited since", first["chunk_id"])
        )
        cur.execute("SELECT content_hash FROM chunks WHERE id = %s", (first["chunk_id"],))
        row = cur.fetchone()

    assert row is not None
    assert row[0] != first["content_hash"]


def test_the_recorded_hash_is_the_one_the_database_computed(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """Computed in Python from content the agent already holds, so it has to
    agree with migration 006's trigger or it proves nothing."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None
    trace = load_trace(migrated, alice, answer.trace_id)
    assert trace is not None

    for item in trace["retrievals"]:
        with migrated.cursor() as cur:
            cur.execute("SELECT content_hash FROM chunks WHERE id = %s", (item["chunk_id"],))
            row = cur.fetchone()
        assert row is not None
        assert row[0] == item["content_hash"]


def test_no_chunk_content_is_copied_into_the_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """§9 says entity ids only. A trace holding content would be a second copy
    of the corpus sitting outside the permission filter."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM trace_retrievals")
        assert (cur.fetchone() or (0,))[0] > 0
        cur.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_name = 'trace_retrievals' AND column_name = 'content'"
        )
        assert cur.fetchone() == (0,)


# ---------------------------------------------------------------------------
# §12 point 2: the filtered path, seen from the trace.
# ---------------------------------------------------------------------------


def test_the_filtered_trace_shows_a_shorter_retrieval_list(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    carol = principal(migrated, slack_id, "U-CAROL")

    alice_answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    carol_answer = build_agent(migrated, ScriptedModel()).answer(migrated, carol, QUESTION, k=40)
    assert alice_answer.trace_id is not None
    assert carol_answer.trace_id is not None

    alice_trace = load_trace(migrated, alice, alice_answer.trace_id)
    carol_trace = load_trace(migrated, carol, carol_answer.trace_id)

    assert alice_trace is not None
    assert carol_trace is not None
    assert len(carol_trace["retrievals"]) < len(alice_trace["retrievals"])


def test_the_filtered_trace_shows_the_same_plan(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """The difference is what the filter returned, not a narrower search. A
    trace that showed a different plan would leave open the reading that the
    query was quietly restricted for this user."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    carol = principal(migrated, slack_id, "U-CAROL")

    a = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    c = build_agent(migrated, ScriptedModel()).answer(migrated, carol, QUESTION, k=40)
    assert a.trace_id is not None
    assert c.trace_id is not None

    alice_trace = load_trace(migrated, alice, a.trace_id)
    carol_trace = load_trace(migrated, carol, c.trace_id)

    assert alice_trace is not None
    assert carol_trace is not None
    assert alice_trace["plan"] == carol_trace["plan"]


def test_the_private_chunk_is_absent_from_the_filtered_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    carol = principal(migrated, slack_id, "U-CAROL")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, carol, QUESTION, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, carol, answer.trace_id)

    assert trace is not None
    hashes = {item["content_hash"] for item in trace["retrievals"]}
    assert content_hash(PRIVATE_TEXT) not in hashes


def test_a_query_with_nothing_visible_is_still_traced(migrated: Connection) -> None:
    """The trace has to explain silence too, and it does so by recording that
    the model was never called."""
    nobody = uuid4()
    with migrated.cursor() as cur:
        cur.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (nobody,))
    model = ScriptedModel()
    answer = Agent(model, embedder=HashingEmbeddings()).answer(migrated, nobody, QUESTION)
    assert answer.trace_id is not None

    trace = load_trace(migrated, nobody, answer.trace_id)

    assert trace is not None
    assert trace["route"] == "nothing_visible"
    assert trace["retrievals"] == []
    assert model.requests == []
    assert trace["steps"][-1]["detail"]["model_called"] is False


# ---------------------------------------------------------------------------
# §12 point 3: the proposal.
# ---------------------------------------------------------------------------


def test_a_proposal_trace_takes_the_propose_route(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    ask = "Add a comment on ACME-1 summarising this"
    marker = _marker(migrated, alice, ask, "jira.issue")
    reply = json.dumps(
        {"action_type": "jira.comment", "source": marker, "payload": {"body": "legal review"}}
    )
    answer = build_agent(migrated, ScriptedModel(reply)).answer(migrated, alice, ask, k=40)
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert trace["route"] == "propose"
    assert step_names(trace) == ["plan", "retrieve", "propose"]


def test_the_trace_links_to_the_action_it_proposed(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """The approval and rollback steps of the demo hang off this join."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    ask = "Add a comment on ACME-1 summarising this"
    marker = _marker(migrated, alice, ask, "jira.issue")
    reply = json.dumps(
        {"action_type": "jira.comment", "source": marker, "payload": {"body": "legal review"}}
    )
    answer = build_agent(migrated, ScriptedModel(reply)).answer(migrated, alice, ask, k=40)
    assert answer.trace_id is not None
    assert answer.proposal is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert UUID(str(trace["action_id"])) == answer.proposal.id
    assert trace["steps"][-1]["detail"]["risk_class"] == "consequential"


def test_a_refused_proposal_is_traced_as_such(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel("NONE")).answer(
        migrated, alice, "Add a comment on ACME-1 summarising this", k=40
    )
    assert answer.trace_id is not None

    trace = load_trace(migrated, alice, answer.trace_id)

    assert trace is not None
    assert trace["action_id"] is None
    assert trace["steps"][-1]["detail"]["proposed"] is False


# ---------------------------------------------------------------------------
# Failure is the case most worth being able to look at afterwards.
# ---------------------------------------------------------------------------


def test_a_failed_query_still_leaves_a_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    agent = build_agent(migrated, ScriptedModel(fail=True))

    with pytest.raises(PermanentProviderError):
        agent.answer(migrated, alice, QUESTION, k=40)

    traces = list_traces(migrated, alice)
    assert len(traces) == 1
    assert traces[0]["route"] == "error"
    assert "PermanentProviderError" in traces[0]["error"]


def test_a_failed_query_traces_the_steps_that_did_run(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    agent = build_agent(migrated, ScriptedModel(fail=True))

    with pytest.raises(PermanentProviderError):
        agent.answer(migrated, alice, QUESTION, k=40)

    trace = load_trace(migrated, alice, UUID(str(list_traces(migrated, alice)[0]["id"])))
    assert trace is not None
    assert step_names(trace) == ["plan", "retrieve"]


def test_recording_never_fails_the_query(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """A trace is a record of something that already happened. Losing it is bad;
    losing the answer with it is worse."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    with migrated.cursor() as cur:
        cur.execute("ALTER TABLE query_traces RENAME TO query_traces_moved")
    try:
        answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
        assert answer.text
    finally:
        migrated.rollback()


def test_recording_reports_the_failure_it_swallowed(migrated: Connection) -> None:
    trace = Trace(id=uuid4(), question="q", plan={}, route="synthesize")

    assert record(migrated, uuid4(), trace) is None
    migrated.rollback()


def test_a_failure_with_tracing_off_records_nothing(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """Turning tracing off turns it off for failures too. A caller that opted
    out should not discover it opted out only when things go well."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    agent = build_agent(migrated, ScriptedModel(fail=True))

    with pytest.raises(PermanentProviderError):
        agent.answer(migrated, alice, QUESTION, k=40, trace=False)

    assert list_traces(migrated, alice) == []


def test_tracing_can_be_turned_off(migrated: Connection, workspace: tuple[UUID, UUID]) -> None:
    """The API layer needs a way to run a query without recording it — a health
    check should not fill the trace log."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")

    build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40, trace=False)

    assert list_traces(migrated, alice) == []


# ---------------------------------------------------------------------------
# Reading a trace is a permission question, answered the same way as the rest.
# ---------------------------------------------------------------------------


def test_you_cannot_read_someone_elses_trace(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    carol = principal(migrated, slack_id, "U-CAROL")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    assert load_trace(migrated, carol, answer.trace_id) is None
    assert list_traces(migrated, carol) == []


def test_a_missing_trace_and_a_forbidden_one_look_the_same(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """A different answer for the second case would confirm that a trace
    exists."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    carol = principal(migrated, slack_id, "U-CAROL")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    assert load_trace(migrated, carol, answer.trace_id) == load_trace(migrated, carol, uuid4())


def test_the_agent_role_can_write_a_trace_and_read_its_own(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """The same shape as the chunk read path: EXECUTE on a filtered function,
    SELECT on nothing."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
    try:
        trace_id = record(
            migrated, alice, Trace(id=uuid4(), question="q", plan={}, route="synthesize")
        )
        assert trace_id is not None
        assert load_trace(migrated, alice, trace_id) is not None
    finally:
        with migrated.cursor() as cur:
            cur.execute("RESET ROLE")


def test_the_agent_role_cannot_select_the_trace_tables(migrated: Connection) -> None:
    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("SELECT * FROM query_traces")
    migrated.rollback()


def test_the_agent_role_cannot_delete_a_trace(migrated: Connection) -> None:
    """An audit log a compromised component can erase is not an audit log."""
    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("DELETE FROM query_traces")
    migrated.rollback()


def test_traces_are_listed_newest_first(migrated: Connection, workspace: tuple[UUID, UUID]) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    agent = build_agent(migrated, ScriptedModel())
    for question in ("first question", "second question", "third question"):
        agent.answer(migrated, alice, question, k=5)

    traces = list_traces(migrated, alice)

    assert traces[0]["question"] == "third question"
    assert len(traces) == 3


def test_the_listing_limit_is_bounded(migrated: Connection, workspace: tuple[UUID, UUID]) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    agent = build_agent(migrated, ScriptedModel())
    for question in ("one", "two", "three"):
        agent.answer(migrated, alice, question, k=5)

    assert len(list_traces(migrated, alice, limit=2)) == 2
    assert len(list_traces(migrated, alice, limit=0)) == 1, "clamped to at least one"


def test_an_unknown_route_is_rejected_by_the_database(migrated: Connection) -> None:
    """The route is what the trace view branches on, so an unrecognised one is
    a bug that should not reach the table."""
    with migrated.cursor() as cur:
        cur.execute("INSERT INTO principals (id, kind) VALUES (%s, 'user')", (p := uuid4(),))
        with pytest.raises(errors.CheckViolation):
            cur.execute(
                "INSERT INTO query_traces (principal_id, question, plan, route) "
                "VALUES (%s, 'q', '{}', 'whatever')",
                (p,),
            )
    migrated.rollback()


def test_deleting_a_trace_takes_its_retrievals(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)

    with migrated.cursor() as cur:
        cur.execute("DELETE FROM query_traces WHERE id = %s", (answer.trace_id,))
        cur.execute("SELECT count(*) FROM trace_retrievals WHERE trace_id = %s", (answer.trace_id,))
        assert cur.fetchone() == (0,)


def _marker(conn: Connection, principal_id: UUID, question: str, source_type: str) -> int:
    from agent.retrieval import plan_query, retrieve

    hits = retrieve(conn, principal_id, plan_query(question, k=40), HashingEmbeddings())
    for index, hit in enumerate(hits, start=1):
        if hit.source_type == source_type:
            return index
    raise AssertionError(f"no {source_type} retrieved")


# ---------------------------------------------------------------------------
# One human, several accounts. Migration 012, found by driving the demo.
# ---------------------------------------------------------------------------


def test_a_trace_is_found_through_any_of_your_accounts(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """The bug this fixes: a login resolves to whichever principal sorts first,
    and the agent records against whichever one asked. Those are often not the
    same account, and the trace list was empty as a result."""
    slack_id, jira_id = workspace
    from resolver.resolution import link_principal_identities

    link_principal_identities(migrated)
    slack_alice = principal(migrated, slack_id, "U-ALICE")
    jira_alice = principal(migrated, jira_id, "u-alice")
    assert slack_alice != jira_alice

    answer = build_agent(migrated, ScriptedModel()).answer(migrated, slack_alice, QUESTION, k=40)
    assert answer.trace_id is not None

    assert load_trace(migrated, jira_alice, answer.trace_id) is not None
    assert [t["id"] for t in list_traces(migrated, jira_alice)] == [answer.trace_id]


def test_ownership_does_not_walk_into_groups(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """A grant to a channel reaches every member; a question does not. Widening
    ownership the way permissions widen would show one person's questions to
    their whole team, which is a different feature and not this one."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT group_id FROM principal_memberships WHERE member_id = %s LIMIT 1", (alice,)
        )
        row = cur.fetchone()
    assert row is not None, "alice is in a group, or this test proves nothing"

    assert load_trace(migrated, UUID(str(row[0])), answer.trace_id) is None


def test_an_unlinked_account_still_sees_only_its_own(
    migrated: Connection, workspace: tuple[UUID, UUID]
) -> None:
    """A NULL identity_id must not behave like a value shared by everyone who
    has not been linked yet."""
    slack_id, _ = workspace
    alice = principal(migrated, slack_id, "U-ALICE")
    carol = principal(migrated, slack_id, "U-CAROL")
    answer = build_agent(migrated, ScriptedModel()).answer(migrated, alice, QUESTION, k=40)
    assert answer.trace_id is not None

    assert load_trace(migrated, carol, answer.trace_id) is None
