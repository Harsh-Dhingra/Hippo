"""P1-SYNC-2's done-condition: private-channel membership gates acl_grants.

This walks the whole path rather than asserting on an intermediate table. Slack
fixtures go in through the connector and the runtime, a stand-in for the
resolver creates entities, the projection fills acl_grants, and then the
permission filter is asked what each person can see.

That last step matters. acl_grants having the right rows is a claim about a
table; visible_chunks returning the right content is the claim the project
actually makes to its users, and it is ARCHITECTURE section 12 point 2 in
miniature: two people ask the same thing, and one of them does not get the
private channel.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from sync.connectors.slack import FixtureTransport, SlackConnector
from sync.runtime import SyncRuntime, project_acl_grants

pytestmark = pytest.mark.requires_db

SLACK_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "slack"
ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

GENERAL_TEXT = "is anything blocking the Acme renewal?"
THREAD_TEXT = "legal review is the blocker, not engineering"
DEALS_TEXT = "Acme is asking for 30 percent off to renew, do not repeat outside this channel"


@pytest.fixture
def connector_id(migrated: Connection) -> UUID:
    new_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
        (new_id,),
    )
    return new_id


def resolve_like_the_resolver(conn: Connection, connector_id: UUID) -> None:
    """Stand-in for P1-RES-1.

    One entity per raw record, linked through entity_sources, which is the
    minimum the projection needs. Everything the real resolver adds on top of
    that (identity merging, summaries) is irrelevant to whether a grant lands
    on the right entity.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, source_type, source_id, payload FROM raw_records WHERE connector_id = %s",
            (connector_id,),
        )
        rows = cur.fetchall()

    for raw_id, source_type, source_id, payload in rows:
        entity_type = str(source_type).rsplit(".", 1)[-1]
        title = payload.get("name") or payload.get("text") or str(source_id)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO entities (entity_type, title) VALUES (%s, %s) RETURNING id",
                (entity_type, title[:200]),
            )
            row = cur.fetchone()
            assert row is not None
            entity_id = row[0]
            cur.execute(
                "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)",
                (entity_id, raw_id),
            )
            # A chunk per message, so the permission filter has something to
            # return. The real chunking policy is P1-RES-3's problem.
            if str(source_type).endswith("message"):
                cur.execute(
                    "INSERT INTO chunks (entity_id, scope_id, content) VALUES (%s, %s, %s)",
                    (entity_id, ORG_SCOPE, payload.get("text", "")),
                )


def principal(conn: Connection, connector_id: UUID, source_id: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals WHERE connector_id = %s AND source_id = %s",
            (connector_id, source_id),
        )
        row = cur.fetchone()
    assert row is not None, f"no principal for {source_id}"
    return UUID(str(row[0]))


def visible_text(conn: Connection, principal_id: UUID) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT content FROM visible_chunks(%s, NULL, NULL, 1000)", (principal_id,))
        return {str(row[0]) for row in cur.fetchall()}


@pytest.fixture
def synced(migrated: Connection, connector_id: UUID) -> Connection:
    runtime = SyncRuntime(SlackConnector(FixtureTransport(SLACK_FIXTURES)), connector_id)
    runtime.sync_all(migrated)
    resolve_like_the_resolver(migrated, connector_id)
    project_acl_grants(migrated, connector_id)
    return migrated


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------


def test_a_private_channel_is_invisible_to_a_non_member(
    synced: Connection, connector_id: UUID
) -> None:
    """Carol is in the workspace and not in #deals-acme."""
    carol = principal(synced, connector_id, "U-CAROL")

    seen = visible_text(synced, carol)

    assert GENERAL_TEXT in seen, "the public channel is readable by the workspace"
    assert DEALS_TEXT not in seen, "the private channel is not"


def test_a_private_channel_is_visible_to_its_members(
    synced: Connection, connector_id: UUID
) -> None:
    """The positive case, so the test above is not passing because nothing is
    visible to anyone."""
    for source_id in ("U-ALICE", "U-BOB"):
        seen = visible_text(synced, principal(synced, connector_id, source_id))
        assert DEALS_TEXT in seen, f"{source_id} is a member of the private channel"
        assert GENERAL_TEXT in seen


def test_thread_replies_inherit_the_channel_grant(synced: Connection, connector_id: UUID) -> None:
    """Replies are separate records; a grant on the channel has to reach them
    or half of every conversation goes missing."""
    seen = visible_text(synced, principal(synced, connector_id, "U-CAROL"))

    assert THREAD_TEXT in seen


def test_grants_land_on_messages_not_only_on_channels(
    synced: Connection, connector_id: UUID
) -> None:
    """The containment walk is what turns one channel grant into coverage of
    everything inside it."""
    with synced.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM acl_grants g "
            "JOIN entities e ON e.id = g.entity_id WHERE e.entity_type = 'message'"
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] > 0


def test_one_source_grant_per_channel_covers_every_message(
    synced: Connection, connector_id: UUID
) -> None:
    """Three source grants, many projected grants. A connector emitting one
    grant per message would not scale past a small workspace."""
    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_source_grants")
        sources = cur.fetchone()
        cur.execute("SELECT count(*) FROM acl_grants")
        projected = cur.fetchone()

    assert sources is not None
    assert projected is not None
    assert sources[0] == 3
    assert projected[0] > sources[0]


# ---------------------------------------------------------------------------
# Revocation, through the whole path.
# ---------------------------------------------------------------------------


@pytest.fixture
def fixtures_without_bob(tmp_path: Path) -> Path:
    """The same workspace, after Bob is removed from the private channel."""
    root = tmp_path / "slack"
    shutil.copytree(SLACK_FIXTURES, root)
    members = root / "conversations.members.C-DEALS.json"
    payload = json.loads(members.read_text(encoding="utf-8"))
    payload[""]["members"] = ["U-ALICE"]
    members.write_text(json.dumps(payload), encoding="utf-8")
    return root


def test_removing_someone_from_a_channel_removes_their_access(
    synced: Connection, connector_id: UUID, fixtures_without_bob: Path
) -> None:
    """The five-minute revocation target in P1-SYNC-4 rests on this being a
    plain consequence of re-syncing ACLs."""
    bob = principal(synced, connector_id, "U-BOB")
    assert DEALS_TEXT in visible_text(synced, bob)

    SyncRuntime(SlackConnector(FixtureTransport(fixtures_without_bob)), connector_id).sync_stream(
        synced, "acls"
    )
    project_acl_grants(synced, connector_id)

    assert DEALS_TEXT not in visible_text(synced, bob)
    assert GENERAL_TEXT in visible_text(synced, bob), "he is still in the workspace"


def test_revocation_does_not_need_the_resolver_to_run_again(
    synced: Connection, connector_id: UUID, fixtures_without_bob: Path
) -> None:
    """Entities and chunks are untouched; only the grants change. This is the
    whole reason ACLs are stored in source terms."""
    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        entities_before = cur.fetchone()

    SyncRuntime(SlackConnector(FixtureTransport(fixtures_without_bob)), connector_id).sync_stream(
        synced, "acls"
    )
    project_acl_grants(synced, connector_id)

    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        entities_after = cur.fetchone()
    assert entities_before == entities_after


def test_projection_is_idempotent(synced: Connection, connector_id: UUID) -> None:
    """It rebuilds rather than diffs, so running it twice must be a no-op."""
    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_grants")
        before = cur.fetchone()

    project_acl_grants(synced, connector_id)

    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_grants")
        after = cur.fetchone()
    assert before == after


def test_projection_leaves_other_connectors_grants_alone(
    synced: Connection, connector_id: UUID
) -> None:
    """One connector rebuilding its grants must not delete another's."""
    other = uuid4()
    alice = principal(synced, connector_id, "U-ALICE")
    with synced.cursor() as cur:
        cur.execute("SELECT id FROM entities LIMIT 1")
        row = cur.fetchone()
        assert row is not None
        cur.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, access, source) "
            "VALUES (%s, %s, 'read', %s)",
            (row[0], alice, str(other)),
        )

    project_acl_grants(synced, connector_id)

    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_grants WHERE source = %s", (str(other),))
        survived = cur.fetchone()
    assert survived is not None
    assert survived[0] == 1


def test_an_unprojected_grant_is_not_a_hole(migrated: Connection, connector_id: UUID) -> None:
    """Before the resolver runs there are no entities, so no chunks, so nothing
    to leak. The grants simply have nowhere to land yet."""
    SyncRuntime(SlackConnector(FixtureTransport(SLACK_FIXTURES)), connector_id).sync_all(migrated)
    project_acl_grants(migrated, connector_id)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_source_grants")
        sources = cur.fetchone()
        cur.execute("SELECT count(*) FROM acl_grants")
        projected = cur.fetchone()
        cur.execute("SELECT count(*) FROM chunks")
        chunks = cur.fetchone()

    assert sources is not None
    assert projected is not None
    assert chunks is not None
    assert sources[0] == 3
    assert projected[0] == 0
    assert chunks[0] == 0
