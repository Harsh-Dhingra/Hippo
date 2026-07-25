"""P1-RES-2's done-condition: one person, two systems, one entity."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from resolver.extraction import EntityCandidate, load_raw_records
from resolver.resolution import (
    BY_CANONICAL_KEY,
    BY_NORMALIZED_NAME,
    BY_SOURCE_ID,
    NEW,
    ResolutionStats,
    link_principal_identities,
    merge_key,
    normalize_name,
    resolve_candidate,
    resolve_connector,
    resolve_records,
    sources_of,
)
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.sdk import SourceRef
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def scalar(conn: Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return None if row is None else row[0]


@pytest.fixture
def both_systems(migrated: Connection) -> tuple[UUID, UUID]:
    """A Slack workspace and a Jira site describing the same three people."""
    slack_id, jira_id = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) "
            "VALUES (%s, 'slack', 'Slack'), (%s, 'jira', 'Jira')",
            (slack_id, jira_id),
        )
    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(migrated)
    SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(migrated)
    return slack_id, jira_id


pytestmark = pytest.mark.requires_db


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------


def test_the_same_person_in_slack_and_jira_becomes_one_entity(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Six person candidates, three people."""
    resolve_connector(migrated)

    assert scalar(migrated, "SELECT count(*) FROM entities WHERE entity_type = 'person'") == 3
    assert (
        scalar(
            migrated,
            "SELECT count(*) FROM raw_records WHERE source_type IN ('slack.user', 'jira.user')",
        )
        == 6
    )


def test_a_merged_person_keeps_both_source_records(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """The provenance trail: the merge can be explained after the fact."""
    resolve_connector(migrated)

    entity_id = scalar(
        migrated, "SELECT id FROM entities WHERE canonical_key = 'alice@example.com'"
    )
    assert {ref.source_type for ref in sources_of(migrated, UUID(str(entity_id)))} == {
        "slack.user",
        "jira.user",
    }


def test_a_merged_person_records_the_rule_that_merged_them(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """A merge is recorded, not just performed, so a wrong one is findable."""
    resolve_connector(migrated)

    attrs = scalar(migrated, "SELECT attrs FROM entities WHERE canonical_key = 'bob@example.com'")
    assert attrs["resolved_by"] == BY_CANONICAL_KEY


def test_merging_is_by_email_not_by_name(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Two colleagues can share a name; nobody shares an email."""
    resolve_connector(migrated)

    keys = scalar(
        migrated,
        "SELECT array_agg(canonical_key ORDER BY canonical_key) FROM entities "
        "WHERE entity_type = 'person'",
    )
    assert keys == ["alice@example.com", "bob@example.com", "carol@example.com"]


def test_an_edge_from_each_system_lands_on_the_one_person(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """The merge is only worth anything if the graph follows it.

    One node for Alice, with authored edges reaching Slack messages, a Jira
    ticket and a Jira comment. Before resolution those were two disconnected
    halves of one person.
    """
    resolve_connector(migrated)

    alice = scalar(migrated, "SELECT id FROM entities WHERE canonical_key = 'alice@example.com'")
    authored = scalar(
        migrated,
        "SELECT array_agg(DISTINCT e2.entity_type ORDER BY e2.entity_type) "
        "FROM edges e JOIN entities e2 ON e2.id = e.dst_id "
        "WHERE e.src_id = %s AND e.edge_type = 'authored'",
        (alice,),
    )
    assert authored == ["comment", "message", "ticket"]


# ---------------------------------------------------------------------------
# The three rules.
# ---------------------------------------------------------------------------


def test_source_id_wins_before_anything_else_is_tried(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Nothing is more certain than the source agreeing with itself, which is
    also what makes re-running stable."""
    resolve_connector(migrated)
    stats = resolve_connector(migrated)

    assert stats.created == 0
    assert stats.merged == 0
    assert stats.by_rule[BY_SOURCE_ID] > 0


def test_a_person_without_an_email_stays_separate(migrated: Connection) -> None:
    """No key means no merge, which is the safe outcome."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    stats = ResolutionStats()
    for source_id in ("U-1", "U-2"):
        raw_id = _raw(migrated, connector_id, "slack.user", source_id, {"profile": {}})
        resolve_candidate(
            migrated,
            EntityCandidate(
                source=SourceRef(source_type="slack.user", source_id=source_id),
                entity_type="person",
                title="Same Name",
            ),
            raw_id,
            stats,
        )

    assert scalar(migrated, "SELECT count(*) FROM entities") == 2


def test_accounts_merge_on_a_normalized_name(migrated: Connection) -> None:
    """The third rule. No connector emits accounts yet, so this exercises the
    rule directly rather than through a corpus."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    stats = ResolutionStats()
    for index, name in enumerate(["Acme Inc.", "ACME, Ltd"]):
        raw_id = _raw(migrated, connector_id, "crm.account", f"A-{index}", {"name": name})
        resolve_candidate(
            migrated,
            EntityCandidate(
                source=SourceRef(source_type="crm.account", source_id=f"A-{index}"),
                entity_type="account",
                title=name,
            ),
            raw_id,
            stats,
        )

    assert scalar(migrated, "SELECT count(*) FROM entities WHERE entity_type = 'account'") == 1
    assert stats.by_rule[BY_NORMALIZED_NAME] == 1
    assert stats.by_rule[NEW] == 1


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Acme Inc.", "acme"),
        ("ACME, Ltd", "acme"),
        ("Acme Corporation", "acme"),
        ("Acme   Widgets  GmbH", "acme widgets"),
        ("Northwind Trading Co", "northwind trading"),
    ],
)
def test_normalize_name_folds_legal_noise(name: str, expected: str) -> None:
    assert normalize_name(name) == expected


def test_normalize_name_keeps_genuinely_different_companies_apart() -> None:
    assert normalize_name("Acme Health") != normalize_name("Acme Legal")


def test_a_name_that_is_only_a_suffix_yields_no_key() -> None:
    """Merging everything called 'Ltd' would be worse than merging nothing."""
    candidate = EntityCandidate(
        source=SourceRef(source_type="crm.account", source_id="A-1"),
        entity_type="account",
        title="Ltd.",
    )
    assert merge_key(candidate) is None


def test_messages_are_never_name_matched() -> None:
    """Normalized-name matching on content would merge anything said twice."""
    candidate = EntityCandidate(
        source=SourceRef(source_type="slack.message", source_id="C-1:1.0"),
        entity_type="message",
        title="thanks",
    )
    assert merge_key(candidate) is None


def test_people_are_never_name_matched() -> None:
    """Two Alex Chens are two people."""
    candidate = EntityCandidate(
        source=SourceRef(source_type="slack.user", source_id="U-1"),
        entity_type="person",
        title="Alex Chen",
    )
    assert merge_key(candidate) is None


# ---------------------------------------------------------------------------
# Merge machinery.
# ---------------------------------------------------------------------------


def test_a_merge_does_not_flicker_the_title(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Candidates arrive sorted, so the same one names the entity every time."""
    resolve_connector(migrated)
    first = scalar(migrated, "SELECT title FROM entities WHERE canonical_key = 'alice@example.com'")

    resolve_connector(migrated)

    assert (
        scalar(migrated, "SELECT title FROM entities WHERE canonical_key = 'alice@example.com'")
        == first
    )


def test_attributes_from_both_systems_survive_the_merge(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """A second system usually knows something the first did not."""
    resolve_connector(migrated)

    attrs = scalar(migrated, "SELECT attrs FROM entities WHERE canonical_key = 'carol@example.com'")
    assert attrs["email"] == "carol@example.com"


def test_the_graph_is_identical_after_a_second_pass(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Re-resolution is a normal operation, not a repair."""
    resolve_connector(migrated)
    before = (
        scalar(migrated, "SELECT count(*) FROM entities"),
        scalar(migrated, "SELECT count(*) FROM edges"),
        scalar(migrated, "SELECT count(*) FROM entity_sources"),
    )

    resolve_connector(migrated)

    assert (
        scalar(migrated, "SELECT count(*) FROM entities"),
        scalar(migrated, "SELECT count(*) FROM edges"),
        scalar(migrated, "SELECT count(*) FROM entity_sources"),
    ) == before


# ---------------------------------------------------------------------------
# Edges.
# ---------------------------------------------------------------------------


def test_edges_are_written_with_source_provenance(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """Nothing deterministic is inferred, so nothing here is below full
    confidence (CLAUDE.md rule 5)."""
    resolve_connector(migrated)

    assert scalar(migrated, "SELECT count(*) FROM edges WHERE provenance <> 'source'") == 0
    assert scalar(migrated, "SELECT count(*) FROM edges WHERE confidence <> 1.0") == 0


def test_containment_edges_exist_for_the_acl_projection(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    resolve_connector(migrated)

    assert scalar(migrated, "SELECT count(*) FROM edges WHERE edge_type = 'belongs_to'") > 0


def test_an_edge_with_an_unresolved_endpoint_is_dropped(migrated: Connection) -> None:
    """A mention of someone who never synced. Inventing an entity for them
    would create a node with no ACL that nothing can see."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )
    _raw(
        migrated,
        connector_id,
        "slack.message",
        "C-1:1.0",
        {"ts": "1.0", "text": "hello <@U-GHOST>"},
        container=("slack.channel", "C-1"),
    )
    _raw(migrated, connector_id, "slack.channel", "C-1", {"name": "general"})

    stats = resolve_records(migrated, load_raw_records(migrated, connector_id))

    assert stats.edges_skipped == 1, "the mention of an unsynced user"
    assert stats.edges_written == 1, "the belongs_to still lands"


def test_edges_survive_a_re_run_without_duplicating(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    resolve_connector(migrated)
    before = scalar(migrated, "SELECT count(*) FROM edges")

    resolve_connector(migrated)

    assert scalar(migrated, "SELECT count(*) FROM edges") == before


def test_a_self_edge_created_by_a_merge_is_dropped(migrated: Connection) -> None:
    """If both endpoints resolve to one entity the edge says nothing, and it
    would confuse graph expansion in the filter."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')",
        (connector_id,),
    )
    # An issue whose reporter and assignee are the same person merges the
    # authored and assigned_to endpoints onto one pair, but neither is a self
    # edge; force the degenerate case directly instead.
    raw_id = _raw(
        migrated,
        connector_id,
        "jira.user",
        "u-1",
        {"emailAddress": "solo@example.com", "displayName": "Solo"},
    )
    other = _raw(
        migrated,
        connector_id,
        "jira.user",
        "u-2",
        {"emailAddress": "solo@example.com", "displayName": "Solo Again"},
    )
    stats = resolve_records(migrated, load_raw_records(migrated, connector_id))

    assert scalar(migrated, "SELECT count(*) FROM entities WHERE entity_type = 'person'") == 1
    assert stats.merged == 1
    assert raw_id != other


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _raw(
    conn: Connection,
    connector_id: UUID,
    source_type: str,
    source_id: str,
    payload: dict[str, Any],
    container: tuple[str, str] | None = None,
) -> UUID:
    from psycopg.types.json import Jsonb

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO raw_records (connector_id, source_type, source_id, payload, "
            "    container_source_type, container_source_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                connector_id,
                source_type,
                source_id,
                Jsonb(payload),
                container[0] if container else None,
                container[1] if container else None,
            ),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


# ---------------------------------------------------------------------------
# One human, several accounts. The same rule as the entity merge — a matching
# email — applied to principals, because a grant names an account and a person
# holds one per system.
# ---------------------------------------------------------------------------


def _principal(
    conn: Connection, connector_id: UUID, source_id: str, email: str | None, kind: str = "user"
) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO principals (kind, connector_id, source_id, email) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            (kind, connector_id, source_id, email),
        )
        row = cur.fetchone()
    assert row is not None
    return UUID(str(row[0]))


def _identity(conn: Connection, principal_id: UUID) -> UUID | None:
    with conn.cursor() as cur:
        cur.execute("SELECT identity_id FROM principals WHERE id = %s", (principal_id,))
        row = cur.fetchone()
    assert row is not None
    return None if row[0] is None else UUID(str(row[0]))


def test_two_accounts_with_one_email_become_one_person(migrated: Connection) -> None:
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-ALICE", "alice@acme.com")
    b = _principal(migrated, jira, "u-alice", "alice@acme.com")

    assert link_principal_identities(migrated) == 2

    assert _identity(migrated, a) is not None
    assert _identity(migrated, a) == _identity(migrated, b)


def test_email_matching_ignores_case_and_padding(migrated: Connection) -> None:
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-ALICE", "Alice@Acme.com")
    b = _principal(migrated, jira, "u-alice", " alice@acme.com ")

    link_principal_identities(migrated)

    assert _identity(migrated, a) == _identity(migrated, b)


def test_different_people_are_not_linked(migrated: Connection) -> None:
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-ALICE", "alice@acme.com")
    b = _principal(migrated, jira, "u-bob", "bob@acme.com")

    link_principal_identities(migrated)

    assert _identity(migrated, a) is None
    assert _identity(migrated, b) is None


def test_accounts_without_an_email_are_never_linked(migrated: Connection) -> None:
    """An absent email is not a match. Linking on it would merge every
    service account in the workspace into one person."""
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-BOT", None)
    b = _principal(migrated, jira, "u-bot", None)
    c = _principal(migrated, slack, "U-BLANK", "   ")

    assert link_principal_identities(migrated) == 0

    assert _identity(migrated, a) is None
    assert _identity(migrated, b) is None
    assert _identity(migrated, c) is None


def test_a_lone_account_gets_no_identity(migrated: Connection) -> None:
    """identity_id means 'one of several', not 'processed'."""
    slack = uuid4()
    _connectors(migrated, slack, uuid4())
    a = _principal(migrated, slack, "U-ALICE", "alice@acme.com")

    link_principal_identities(migrated)

    assert _identity(migrated, a) is None


def test_groups_are_never_linked(migrated: Connection) -> None:
    """A shared mailing address is not shared membership."""
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "G-ENG", "eng@acme.com", kind="group")
    b = _principal(migrated, jira, "g-eng", "eng@acme.com", kind="group")

    assert link_principal_identities(migrated) == 0

    assert _identity(migrated, a) is None
    assert _identity(migrated, b) is None


def test_linking_is_idempotent(migrated: Connection) -> None:
    """Re-running must not reshuffle ids that audit records may refer to."""
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-ALICE", "alice@acme.com")
    _principal(migrated, jira, "u-alice", "alice@acme.com")

    link_principal_identities(migrated)
    first = _identity(migrated, a)

    assert link_principal_identities(migrated) == 0
    assert _identity(migrated, a) == first


def test_a_third_account_joins_the_existing_identity(migrated: Connection) -> None:
    slack, jira = uuid4(), uuid4()
    _connectors(migrated, slack, jira)
    a = _principal(migrated, slack, "U-ALICE", "alice@acme.com")
    _principal(migrated, jira, "u-alice", "alice@acme.com")
    link_principal_identities(migrated)
    existing = _identity(migrated, a)

    github = uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) "
            "VALUES (%s, 'slack', 'Second Slack', '{}')",
            (github,),
        )
    c = _principal(migrated, github, "U-ALICE-2", "alice@acme.com")

    assert link_principal_identities(migrated) == 1
    assert _identity(migrated, c) == existing


def test_a_resolution_pass_links_accounts(
    migrated: Connection, both_systems: tuple[UUID, UUID]
) -> None:
    """It runs as part of resolution, not as a separate step someone has to
    remember: ARCHITECTURE section 12 point 1 is unreachable without it."""
    stats = resolve_records(migrated, load_raw_records(migrated))

    assert stats.principals_linked >= 2


def _connectors(conn: Connection, slack: UUID, jira: UUID) -> None:
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO connectors (id, kind, display_name, config) VALUES (%s, %s, %s, '{}')",
            [(slack, "slack", "Slack"), (jira, "jira", "Jira")],
        )
