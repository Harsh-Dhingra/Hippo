"""Schema constraints that carry a rule, not just a type.

Several of the non-negotiable rules in CLAUDE.md are enforceable by the
database. Where they are, the database is the enforcement and this file is the
proof: an executed action cannot exist without its inverse, a model-inferred
edge cannot claim certainty, and a scope cannot be owned by the wrong kind of
principal.
"""

import pytest
from psycopg import errors

from core.db import Connection

pytestmark = pytest.mark.requires_db

CONNECTOR = "11111111-1111-1111-1111-111111111111"
USER = "22222222-2222-2222-2222-222222222222"
GROUP = "33333333-3333-3333-3333-333333333333"
ORG_SCOPE = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def graph(migrated: Connection) -> Connection:
    """A connector, a user principal, a group principal and two entities."""
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'Jira')",
            (CONNECTOR,),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id, email) "
            "VALUES (%s, 'user', %s, 'u1', 'ada@example.com')",
            (USER, CONNECTOR),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id) "
            "VALUES (%s, 'group', %s, 'g1')",
            (GROUP, CONNECTOR),
        )
        cur.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES "
            "('44444444-4444-4444-4444-444444444444', 'ticket', 'JIRA-123'), "
            "('55555555-5555-5555-5555-555555555555', 'person', 'Ada')"
        )
    return migrated


SRC = "44444444-4444-4444-4444-444444444444"
DST = "55555555-5555-5555-5555-555555555555"


def _insert_action(conn: Connection, **overrides: object) -> None:
    row: dict[str, object] = {
        "requested_by": USER,
        "connector_id": CONNECTOR,
        "action_type": "jira.comment",
        "payload": "{}",
        "risk_class": "consequential",
        "status": "pending",
        "approved_by": None,
        "inverse_payload": None,
        "executed_at": None,
    }
    row.update(overrides)
    columns = ", ".join(row)
    placeholders = ", ".join(["%s"] * len(row))
    conn.execute(
        f"INSERT INTO actions ({columns}) VALUES ({placeholders})",
        tuple(row.values()),
    )


# ---------------------------------------------------------------------------
# Rule 3: inverse before execution.
# ---------------------------------------------------------------------------


def test_executed_action_without_an_inverse_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="inverse_captured_before_execution"):
        _insert_action(
            graph, status="executed", approved_by=USER, executed_at="2026-07-24T00:00:00Z"
        )


def test_rolled_back_action_without_an_inverse_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="inverse_captured_before_execution"):
        _insert_action(
            graph, status="rolled_back", approved_by=USER, executed_at="2026-07-24T00:00:00Z"
        )


def test_executed_action_with_an_inverse_is_accepted(graph: Connection) -> None:
    _insert_action(
        graph,
        status="executed",
        approved_by=USER,
        inverse_payload='{"comment_id": "10001"}',
        executed_at="2026-07-24T00:00:00Z",
    )
    with graph.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE status = 'executed'")
        assert cur.fetchone() == (1,)


def test_executed_action_must_record_when(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="executed_has_timestamp"):
        _insert_action(graph, status="executed", approved_by=USER, inverse_payload="{}")


# ---------------------------------------------------------------------------
# Rule 2: nothing executes without a human on the record.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["approved", "executed", "rolled_back"])
def test_action_beyond_pending_requires_an_approver(graph: Connection, status: str) -> None:
    with pytest.raises(errors.CheckViolation, match="execution_requires_approval"):
        _insert_action(
            graph,
            status=status,
            inverse_payload="{}",
            executed_at="2026-07-24T00:00:00Z",
        )


def test_pending_action_needs_no_approver(graph: Connection) -> None:
    _insert_action(graph, status="pending")


def test_failed_action_needs_no_approver(graph: Connection) -> None:
    """A proposal can fail before anyone looks at it."""
    _insert_action(graph, status="failed")


@pytest.mark.parametrize("status", ["deleted", "PENDING", "", "done"])
def test_unknown_action_status_is_rejected(graph: Connection, status: str) -> None:
    # approved_by is set so the approval constraint is satisfied and the status
    # check is the only one that can fire.
    with pytest.raises(errors.CheckViolation, match="status_known"):
        _insert_action(graph, status=status, approved_by=USER)


def test_unknown_risk_class_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="risk_class_known"):
        _insert_action(graph, risk_class="trivial")


# ---------------------------------------------------------------------------
# Rule 5: provenance always, and inferred is never certain.
# ---------------------------------------------------------------------------


def test_model_edge_cannot_claim_full_confidence(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="model_is_never_certain"):
        graph.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
            "VALUES (%s, %s, 'mentions', 'model', 1.0)",
            (SRC, DST),
        )


def test_model_edge_below_full_confidence_is_accepted(graph: Connection) -> None:
    graph.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
        "VALUES (%s, %s, 'mentions', 'model', 0.82)",
        (SRC, DST),
    )


def test_source_edge_and_model_edge_can_coexist(graph: Connection) -> None:
    """Provenance is part of edge identity, so an inference cannot silently
    overwrite the deterministic fact it duplicates."""
    graph.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
        "VALUES (%s, %s, 'mentions', 'source', 1.0)",
        (SRC, DST),
    )
    graph.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
        "VALUES (%s, %s, 'mentions', 'model', 0.4)",
        (SRC, DST),
    )

    with graph.cursor() as cur:
        cur.execute("SELECT provenance FROM edges ORDER BY provenance")
        assert [row[0] for row in cur.fetchall()] == ["model", "source"]


def test_duplicate_edge_of_the_same_provenance_is_rejected(graph: Connection) -> None:
    graph.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'mentions', 'source')",
        (SRC, DST),
    )
    with pytest.raises(errors.UniqueViolation):
        graph.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "VALUES (%s, %s, 'mentions', 'source')",
            (SRC, DST),
        )


def test_unknown_provenance_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="provenance_known"):
        graph.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "VALUES (%s, %s, 'mentions', 'vibes')",
            (SRC, DST),
        )


@pytest.mark.parametrize("confidence", [0.0, -0.5, 1.5])
def test_confidence_outside_the_unit_range_is_rejected(
    graph: Connection, confidence: float
) -> None:
    with pytest.raises(errors.CheckViolation, match="confidence_in_range"):
        graph.execute(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance, confidence) "
            "VALUES (%s, %s, 'mentions', 'resolver', %s)",
            (SRC, DST, confidence),
        )


# ---------------------------------------------------------------------------
# Scope ownership is structural (D2), so the filter's scope check has one meaning.
# ---------------------------------------------------------------------------


def test_org_scope_is_seeded_with_a_fixed_id(migrated: Connection) -> None:
    with migrated.cursor() as cur:
        cur.execute("SELECT scope_type, name FROM memory_scopes WHERE id = %s", (ORG_SCOPE,))
        assert cur.fetchone() == ("org", "Organization")


def test_team_scope_must_be_owned_by_a_group(graph: Connection) -> None:
    graph.execute(
        "INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name) "
        "VALUES ('team', %s, 'group', 'Platform')",
        (GROUP,),
    )


def test_team_scope_owned_by_a_user_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="owner_matches_type"):
        graph.execute(
            "INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name) "
            "VALUES ('team', %s, 'user', 'Platform')",
            (USER,),
        )


def test_personal_scope_owned_by_a_group_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="owner_matches_type"):
        graph.execute(
            "INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name) "
            "VALUES ('personal', %s, 'group', 'Ada')",
            (GROUP,),
        )


def test_owner_kind_must_match_the_principal(graph: Connection) -> None:
    """Claiming a user is a group does not make it one: the composite foreign
    key checks the claim against principals."""
    with pytest.raises(errors.ForeignKeyViolation):
        graph.execute(
            "INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name) "
            "VALUES ('team', %s, 'group', 'Platform')",
            (USER,),
        )


def test_org_scope_cannot_have_an_owner(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="owner_matches_type"):
        graph.execute(
            "INSERT INTO memory_scopes (scope_type, owner_principal, owner_kind, name) "
            "VALUES ('org', %s, 'group', 'Everyone')",
            (GROUP,),
        )


def test_personal_scope_without_an_owner_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="owner_matches_type"):
        graph.execute("INSERT INTO memory_scopes (scope_type, name) VALUES ('personal', 'Ada')")


# ---------------------------------------------------------------------------
# Principal identity (D5).
# ---------------------------------------------------------------------------


def test_same_connector_and_source_id_cannot_repeat(graph: Connection) -> None:
    with pytest.raises(errors.UniqueViolation):
        graph.execute(
            "INSERT INTO principals (kind, connector_id, source_id) VALUES ('user', %s, 'u1')",
            (CONNECTOR,),
        )


def test_one_email_may_appear_once_per_connector(graph: Connection) -> None:
    """The RES-2 merge case: the same human in two systems is two principals."""
    graph.execute(
        "INSERT INTO connectors (id, kind, display_name) "
        "VALUES ('66666666-6666-6666-6666-666666666666', 'slack', 'Slack')"
    )
    graph.execute(
        "INSERT INTO principals (kind, connector_id, source_id, email) VALUES "
        "('user', '66666666-6666-6666-6666-666666666666', 'U99', 'ada@example.com')"
    )

    with graph.cursor() as cur:
        cur.execute("SELECT count(*) FROM principals WHERE lower(email) = 'ada@example.com'")
        assert cur.fetchone() == (2,)


def test_platform_principals_cannot_share_an_email(graph: Connection) -> None:
    """With no connector there is no source id to disambiguate, so email is the key."""
    graph.execute("INSERT INTO principals (kind, email) VALUES ('user', 'ops@example.com')")
    with pytest.raises(errors.UniqueViolation):
        graph.execute("INSERT INTO principals (kind, email) VALUES ('user', 'OPS@example.com')")


def test_unknown_principal_kind_is_rejected(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="kind_known"):
        graph.execute("INSERT INTO principals (kind) VALUES ('service')")


def test_a_group_cannot_contain_itself(graph: Connection) -> None:
    with pytest.raises(errors.CheckViolation, match="no_self_loop"):
        graph.execute(
            "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s)",
            (GROUP, GROUP),
        )


# ---------------------------------------------------------------------------
# Retrieval-layer plumbing (D3).
# ---------------------------------------------------------------------------


def test_chunk_full_text_vector_is_generated_from_content(graph: Connection) -> None:
    graph.execute(
        "INSERT INTO chunks (entity_id, scope_id, content) "
        "VALUES (%s, %s, 'the renewal is blocked on pricing approval')",
        (SRC, ORG_SCOPE),
    )

    with graph.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM chunks "
            "WHERE content_tsv @@ plainto_tsquery('english', 'blocking')"
        )
        assert cur.fetchone() == (1,), "stemming should match 'blocked' against 'blocking'"


def test_chunk_full_text_vector_follows_edits(graph: Connection) -> None:
    """Generated, not trigger-fed, so it cannot drift from content."""
    graph.execute(
        "INSERT INTO chunks (id, entity_id, scope_id, content) "
        "VALUES ('77777777-7777-7777-7777-777777777777', %s, %s, 'pricing')",
        (SRC, ORG_SCOPE),
    )
    graph.execute(
        "UPDATE chunks SET content = 'staffing' WHERE id = '77777777-7777-7777-7777-777777777777'"
    )

    with graph.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM chunks WHERE content_tsv @@ plainto_tsquery('english', 'pricing')"
        )
        assert cur.fetchone() == (0,)


def test_chunk_cannot_be_written_without_a_scope(graph: Connection) -> None:
    with pytest.raises(errors.NotNullViolation):
        graph.execute("INSERT INTO chunks (entity_id, content) VALUES (%s, 'orphan')", (SRC,))


# ---------------------------------------------------------------------------
# Housekeeping.
# ---------------------------------------------------------------------------


def test_updating_an_entity_advances_updated_at(graph: Connection) -> None:
    with graph.cursor() as cur:
        cur.execute("SELECT created_at, updated_at FROM entities WHERE id = %s", (SRC,))
        before = cur.fetchone()
    assert before is not None

    graph.execute("UPDATE entities SET summary = 'regenerated' WHERE id = %s", (SRC,))

    with graph.cursor() as cur:
        cur.execute("SELECT updated_at FROM entities WHERE id = %s", (SRC,))
        after = cur.fetchone()
    assert after is not None
    assert after[0] > before[1]


def test_pgvector_and_hnsw_index_are_present(migrated: Connection) -> None:
    with migrated.cursor() as cur:
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT count(*) FROM pg_indexes WHERE tablename = 'chunks' AND indexdef LIKE '%hnsw%'"
        )
        assert cur.fetchone() == (1,)
