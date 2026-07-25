"""P1-RES-3's done-condition: a re-run changes nothing it should not.

No duplicate chunks, summaries replaced rather than appended, and unchanged
text keeping the embedding it already has, which is what makes re-embedding a
resolver re-run rather than an outage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from core.config import Settings
from core.db import Connection
from resolver.chunking import Chunk, adf_text, chunks_for, split_text
from resolver.embeddings import (
    EmbeddingError,
    HashingEmbeddings,
    OpenAICompatibleEmbeddings,
    build_provider,
    to_pgvector,
)
from resolver.enrichment import EnrichmentStats, desired_chunks, enrich_all, enrichable_entities
from resolver.resolution import resolve_connector
from resolver.summaries import ExtractiveSummarizer
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def scalar(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


# ---------------------------------------------------------------------------
# Chunking policies. Pure functions.
# ---------------------------------------------------------------------------


def test_a_slack_message_is_one_chunk() -> None:
    chunks = chunks_for("slack.message", {"text": "the renewal is blocked"})

    assert [c.content for c in chunks] == ["the renewal is blocked"]


def test_a_jira_issue_is_chunked_per_field() -> None:
    """A summary and a description answer different questions; fusing them
    buries the shorter one."""
    chunks = chunks_for(
        "jira.issue",
        {"fields": {"summary": "Renewal blocked", "description": "Legal flagged the cap."}},
    )

    assert [c.content for c in chunks] == ["Renewal blocked", "Legal flagged the cap."]
    assert [c.index for c in chunks] == [0, 1]


def test_an_empty_field_contributes_no_chunk() -> None:
    chunks = chunks_for("jira.issue", {"fields": {"summary": "Only a summary"}})

    assert len(chunks) == 1


def test_rich_text_descriptions_are_flattened() -> None:
    """Real Jira sends a document tree, and the substance of a ticket is
    usually in it."""
    chunks = chunks_for(
        "jira.issue",
        {
            "fields": {
                "summary": "s",
                "description": {
                    "type": "doc",
                    "content": [
                        {
                            "type": "paragraph",
                            "content": [
                                {"type": "text", "text": "Our floor is"},
                                {"type": "text", "text": " eighteen percent."},
                            ],
                        }
                    ],
                },
            }
        },
    )

    assert chunks[1].content == "Our floor is eighteen percent."


def test_adf_text_handles_the_shapes_jira_sends() -> None:
    assert adf_text("plain") == "plain"
    assert adf_text(None) == ""
    assert adf_text({"type": "doc", "content": []}) == ""


def test_containers_and_people_are_not_chunked() -> None:
    """Rows in the retrieval path that no answer would ever cite."""
    for source_type in ("slack.channel", "slack.user", "jira.project", "jira.user"):
        assert chunks_for(source_type, {"name": "x", "text": "y"}) == []


def test_an_unknown_source_type_is_not_chunked() -> None:
    assert chunks_for("github.pull_request", {"body": "text"}) == []


def test_long_text_is_split_with_overlap() -> None:
    text = " ".join(f"word{n}" for n in range(2000))

    windows = split_text(text, max_chars=500, overlap=100)

    assert len(windows) > 1
    assert all(len(window) <= 500 for window in windows)
    assert windows[1][:50] in text


def test_splitting_does_not_cut_words_in_half() -> None:
    text = " ".join(f"word{n}" for n in range(500))

    for window in split_text(text, max_chars=200, overlap=20):
        assert not window.startswith("ord")


def test_splitting_terminates_on_unbroken_text() -> None:
    """A base64 blob has no whitespace to back off to."""
    windows = split_text("x" * 5000, max_chars=300, overlap=50)

    assert len(windows) > 1
    assert "".join(windows)


def test_whitespace_is_normalised_before_hashing() -> None:
    """Two spellings of the same text must not be two chunks."""
    first = chunks_for("slack.message", {"text": "a  b\n c"})
    second = chunks_for("slack.message", {"text": "a b c"})

    assert first[0].content_hash == second[0].content_hash


def test_a_chunk_is_identified_by_what_it_says() -> None:
    assert (
        Chunk(content="same", index=0).content_hash == Chunk(content="same", index=7).content_hash
    )
    assert Chunk(content="a", index=0).content_hash != Chunk(content="b", index=0).content_hash


# ---------------------------------------------------------------------------
# Embedding providers.
# ---------------------------------------------------------------------------


def test_hashing_embeddings_are_deterministic() -> None:
    provider = HashingEmbeddings()

    assert provider.embed(["renewal blocked"]) == provider.embed(["renewal blocked"])


def test_hashing_embeddings_are_unit_vectors_of_the_right_width() -> None:
    (vector,) = HashingEmbeddings(dimensions=1024).embed(["some text here"])

    assert len(vector) == 1024
    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_hashing_embeddings_place_shared_words_closer() -> None:
    """Crude lexical similarity, which is all it claims."""
    provider = HashingEmbeddings()
    base, near, far = provider.embed(
        ["acme renewal pricing", "acme renewal blocked", "unrelated kitchen appliance"]
    )

    def dot(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert dot(base, near) > dot(base, far)


def test_empty_text_does_not_produce_a_zero_vector() -> None:
    """Cosine distance against a zero vector is undefined."""
    (vector,) = HashingEmbeddings().embed([""])

    assert abs(sum(value * value for value in vector) - 1.0) < 1e-9


def test_hashing_embeddings_reject_nonsense_dimensions() -> None:
    with pytest.raises(ValueError, match="dimensions"):
        HashingEmbeddings(dimensions=0)


def _openai(handler: Any, **kwargs: Any) -> OpenAICompatibleEmbeddings:
    return OpenAICompatibleEmbeddings(
        "some-embedding-model",
        dimensions=3,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_openai_compatible_provider_sends_the_expected_request() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        seen["body"] = request.read().decode()
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    provider = _openai(handler, base_url="http://localhost:11434/v1", api_key="secret")
    assert provider.embed(["hello"]) == [[1.0, 0.0, 0.0]]

    assert seen["url"] == "http://localhost:11434/v1/embeddings"
    assert seen["auth"] == "Bearer secret"
    assert "some-embedding-model" in seen["body"]


def test_a_local_endpoint_needs_no_api_key() -> None:
    """Ollama and vLLM do not want one, and sending an empty bearer confuses
    some of them."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0, 0.0]}]})

    _openai(handler).embed(["hello"])

    assert seen["auth"] is None


def test_out_of_order_responses_are_realigned() -> None:
    """The index field is what says which input a vector belongs to."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            },
        )

    assert _openai(handler).embed(["a", "b"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]


def test_requests_are_batched() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        inputs = json.loads(request.read())["input"]
        calls.append(len(inputs))
        return httpx.Response(
            200,
            json={"data": [{"index": i, "embedding": [1.0, 0.0, 0.0]} for i in range(len(inputs))]},
        )

    _openai(handler, batch_size=2).embed(["a", "b", "c", "d", "e"])

    assert calls == [2, 2, 1]


def test_embedding_nothing_calls_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
        raise AssertionError("no request should be made")

    assert _openai(handler).embed([]) == []


def test_a_wrong_width_is_refused_loudly() -> None:
    """The chunks column is a fixed-width vector, so a mismatched model has to
    fail rather than write garbage."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 0.0]}]})

    with pytest.raises(EmbeddingError, match="dimensions"):
        _openai(handler).embed(["hello"])


def test_an_http_error_becomes_an_embedding_error() -> None:
    with pytest.raises(EmbeddingError, match="503"):
        _openai(lambda request: httpx.Response(503, text="down")).embed(["hello"])


def test_a_malformed_response_becomes_an_embedding_error() -> None:
    with pytest.raises(EmbeddingError, match="no data array"):
        _openai(lambda request: httpx.Response(200, json={"oops": True})).embed(["hello"])


def test_a_row_without_an_embedding_becomes_an_embedding_error() -> None:
    with pytest.raises(EmbeddingError, match="no embedding"):
        _openai(lambda request: httpx.Response(200, json={"data": [{"index": 0}]})).embed(["hi"])


def test_a_network_failure_becomes_an_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingError, match="request failed"):
        _openai(handler).embed(["hello"])


def test_a_short_response_becomes_an_embedding_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(EmbeddingError, match="expected 2 embeddings"):
        _openai(handler).embed(["a", "b"])


def test_an_openai_provider_must_name_a_model() -> None:
    with pytest.raises(ValueError, match="embedding model must be named"):
        OpenAICompatibleEmbeddings("")


def test_the_default_configuration_needs_no_service() -> None:
    """Someone should be able to run this before choosing a model."""
    provider = build_provider(Settings(_env_file=None))  # type: ignore[call-arg]

    assert isinstance(provider, HashingEmbeddings)
    assert provider.dimensions == 1024


def test_choosing_openai_without_a_model_is_rejected_in_config() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="embedding_model is required"):
        Settings(embedding_provider="openai", _env_file=None)  # type: ignore[call-arg]


def test_configuring_openai_builds_the_http_provider() -> None:
    provider = build_provider(
        Settings(  # type: ignore[call-arg]
            embedding_provider="openai",
            embedding_model="a-model",
            _env_file=None,
        )
    )

    assert isinstance(provider, OpenAICompatibleEmbeddings)
    assert provider.model == "a-model"


def test_pgvector_formatting() -> None:
    assert to_pgvector([1.0, -0.5]) == "[1.0,-0.5]"


# ---------------------------------------------------------------------------
# Summaries.
# ---------------------------------------------------------------------------


def test_the_extractive_summary_opens_each_chunk() -> None:
    summary = ExtractiveSummarizer().summarize(
        "Ticket", ["Legal flagged the cap. More detail here.", "Engineering is done."]
    )

    assert summary == "Legal flagged the cap. Engineering is done."


def test_a_summary_of_nothing_is_nothing() -> None:
    """Returning the title would duplicate a column that already exists."""
    assert ExtractiveSummarizer().summarize("Ticket", []) is None
    assert ExtractiveSummarizer().summarize("Ticket", ["   "]) is None


def test_summaries_stay_within_their_budget() -> None:
    summary = ExtractiveSummarizer(budget=50).summarize("t", ["word " * 200])

    assert summary is not None
    assert len(summary) <= 50


def test_repeated_chunks_are_not_repeated_in_the_summary() -> None:
    summary = ExtractiveSummarizer().summarize("t", ["Same thing.", "Same thing."])

    assert summary == "Same thing."


# ---------------------------------------------------------------------------
# The done-condition, against a database.
# ---------------------------------------------------------------------------

pytestmark_db = pytest.mark.requires_db


@pytest.fixture
def resolved(migrated: Connection) -> tuple[UUID, UUID]:
    """Both connectors synced, extracted and resolved. Ready to enrich."""
    slack_id, jira_id = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) "
            "VALUES (%s, 'slack', 'Slack'), (%s, 'jira', 'Jira')",
            (slack_id, jira_id),
        )
    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(migrated)
    SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(migrated)
    resolve_connector(migrated)
    return slack_id, jira_id


def enrich(conn: Connection) -> EnrichmentStats:
    return enrich_all(conn, HashingEmbeddings(), ExtractiveSummarizer())


@pytest.mark.requires_db
def test_re_running_creates_no_duplicate_chunks(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """The done-condition."""
    first = enrich(migrated)
    count = scalar(migrated, "SELECT count(*) FROM chunks")

    second = enrich(migrated)

    assert first.chunks_created > 0
    assert second.chunks_created == 0
    assert second.chunks_removed == 0
    assert scalar(migrated, "SELECT count(*) FROM chunks") == count


@pytest.mark.requires_db
def test_a_re_run_changes_nothing_at_all(migrated: Connection, resolved: tuple[UUID, UUID]) -> None:
    enrich(migrated)

    assert enrich(migrated).unchanged is True


@pytest.mark.requires_db
def test_unchanged_text_keeps_the_embedding_it_already_has(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """This is what makes re-embedding affordable, and therefore what makes
    swapping the model a resolver re-run rather than an outage."""
    enrich(migrated)

    assert enrich(migrated).chunks_embedded == 0


@pytest.mark.requires_db
def test_every_chunk_gets_embedded(migrated: Connection, resolved: tuple[UUID, UUID]) -> None:
    enrich(migrated)

    assert scalar(migrated, "SELECT count(*) FROM chunks WHERE embedding IS NULL") == 0


@pytest.mark.requires_db
def test_summaries_are_replaced_not_appended(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """The other half of the done-condition. A stale summary must not survive."""
    enrich(migrated)
    entity_id = scalar(migrated, "SELECT id FROM entities WHERE summary IS NOT NULL LIMIT 1")
    original = scalar(migrated, "SELECT summary FROM entities WHERE id = %s", (entity_id,))

    migrated.execute(
        "UPDATE entities SET summary = 'a stale summary from an older policy' WHERE id = %s",
        (entity_id,),
    )
    stats = enrich(migrated)

    assert stats.summaries_written == 1
    assert scalar(migrated, "SELECT summary FROM entities WHERE id = %s", (entity_id,)) == original


@pytest.mark.requires_db
def test_changed_text_replaces_its_chunk_and_is_re_embedded(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """An edited message: the old chunk goes, the new one arrives, and
    retrieval never sees the text that no longer exists."""
    enrich(migrated)
    migrated.execute(
        "UPDATE raw_records SET payload = jsonb_set(payload, '{text}', '\"edited entirely\"') "
        "WHERE source_type = 'slack.message' AND source_id = 'C-GENERAL:1750000050.000100'"
    )

    stats = enrich(migrated)

    assert stats.chunks_created == 1
    assert stats.chunks_removed == 1
    assert stats.chunks_embedded == 1
    assert (
        scalar(
            migrated,
            "SELECT count(*) FROM chunks WHERE content = 'JIRA-123 tracks the integration work'",
        )
        == 0
    )
    assert scalar(migrated, "SELECT count(*) FROM chunks WHERE content = 'edited entirely'") == 1


@pytest.mark.requires_db
def test_deleted_text_removes_its_chunk(migrated: Connection, resolved: tuple[UUID, UUID]) -> None:
    enrich(migrated)
    before = scalar(migrated, "SELECT count(*) FROM chunks")

    migrated.execute(
        "UPDATE raw_records SET payload = payload - 'text' WHERE source_type = 'slack.message'"
    )
    enrich(migrated)

    assert scalar(migrated, "SELECT count(*) FROM chunks") < before


@pytest.mark.requires_db
def test_a_jira_issue_contributes_a_chunk_per_field(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    enrich(migrated)

    entity_id = scalar(
        migrated,
        "SELECT id FROM entities WHERE entity_type = 'ticket' AND title LIKE %s",
        ("Acme%",),
    )
    contents = scalar(
        migrated,
        "SELECT array_agg(content ORDER BY chunk_index) FROM chunks WHERE entity_id = %s",
        (entity_id,),
    )
    assert contents == [
        "Acme renewal blocked on legal review",
        "Legal have flagged the liability cap. Nothing ships until that clause is agreed.",
    ]


@pytest.mark.requires_db
def test_only_content_entities_are_chunked(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    enrich(migrated)

    types = scalar(
        migrated,
        "SELECT array_agg(DISTINCT e.entity_type ORDER BY e.entity_type) "
        "FROM chunks c JOIN entities e ON e.id = c.entity_id",
    )
    assert types == ["comment", "message", "ticket"]


@pytest.mark.requires_db
def test_enrichment_can_be_scoped_to_one_connector(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    slack_id, _jira_id = resolved

    enrich_all(migrated, HashingEmbeddings(), ExtractiveSummarizer(), connector_id=slack_id)

    types = scalar(
        migrated,
        "SELECT array_agg(DISTINCT e.entity_type ORDER BY e.entity_type) "
        "FROM chunks c JOIN entities e ON e.id = c.entity_id",
    )
    assert types == ["message"]


@pytest.mark.requires_db
def test_chunks_land_in_the_org_scope(migrated: Connection, resolved: tuple[UUID, UUID]) -> None:
    enrich(migrated)

    assert (
        scalar(
            migrated,
            "SELECT count(DISTINCT scope_id) FROM chunks",
        )
        == 1
    )


@pytest.mark.requires_db
def test_the_same_text_twice_in_one_entity_is_one_chunk(migrated: Connection) -> None:
    """A ticket whose summary repeats its description should be retrieved once."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')", (connector_id,)
    )
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_records (connector_id, source_type, source_id, payload) "
            "VALUES (%s, 'jira.issue', 'D-1', "
            '\'{"fields": {"summary": "same", "description": "same"}}\') RETURNING id',
            (connector_id,),
        )
        row = cur.fetchone()
    assert row is not None
    resolve_connector(migrated, connector_id)

    enrich(migrated)

    assert scalar(migrated, "SELECT count(*) FROM chunks") == 1


@pytest.mark.requires_db
def test_python_and_postgres_agree_on_the_content_hash(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """The trigger owns the column and the resolver computes the same value to
    diff against it. If those ever disagreed, every re-run would re-embed
    everything and never notice."""
    enrich(migrated)

    with migrated.cursor() as cur:
        cur.execute("SELECT content, content_hash FROM chunks")
        rows = cur.fetchall()

    assert rows
    for content, stored in rows:
        assert Chunk(content=str(content), index=0).content_hash == str(stored)


@pytest.mark.requires_db
def test_the_hash_follows_an_edited_chunk(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    """The trigger fires on update too, so no writer can leave a stale hash."""
    enrich(migrated)
    chunk_id = scalar(migrated, "SELECT id FROM chunks LIMIT 1")

    migrated.execute("UPDATE chunks SET content = 'something else' WHERE id = %s", (chunk_id,))

    assert (
        scalar(migrated, "SELECT content_hash FROM chunks WHERE id = %s", (chunk_id,))
        == Chunk(content="something else", index=0).content_hash
    )


@pytest.mark.requires_db
def test_desired_chunks_reads_the_raw_records_behind_an_entity(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    entity_id = scalar(migrated, "SELECT id FROM entities WHERE entity_type = 'comment' LIMIT 1")

    assert desired_chunks(migrated, UUID(str(entity_id)))


@pytest.mark.requires_db
def test_only_entities_with_a_policy_are_enriched(
    migrated: Connection, resolved: tuple[UUID, UUID]
) -> None:
    candidates = enrichable_entities(migrated)
    total = scalar(migrated, "SELECT count(*) FROM entities")

    assert 0 < len(candidates) < total
