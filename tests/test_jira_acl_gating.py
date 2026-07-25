"""P1-SYNC-3's done-condition: project-role membership gates acl_grants.

Same standard as Slack, one level deeper. A Jira grant is on the project, and
what has to become invisible is a comment on an issue in that project, so the
containment walk has to carry the grant across two hops rather than one.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from core.db import Connection
from sync.connectors.jira import FixtureTransport, JiraConnector
from sync.runtime import SyncRuntime, project_acl_grants
from tests.pipeline import principal, resolve_and_enrich, visible_text

pytestmark = pytest.mark.requires_db

JIRA_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jira"

ACME_SUMMARY = "Acme renewal blocked on legal review"
ACME_COMMENT = "legal will not sign until the liability cap is agreed"
PUB_SUMMARY = "Publish the integration guide"
PUB_COMMENT = "guide is live on the docs site"


@pytest.fixture
def connector_id(migrated: Connection) -> UUID:
    new_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'Jira')",
        (new_id,),
    )
    return new_id


@pytest.fixture
def synced(migrated: Connection, connector_id: UUID) -> Connection:
    SyncRuntime(JiraConnector(FixtureTransport(JIRA_FIXTURES)), connector_id).sync_all(migrated)
    resolve_and_enrich(migrated, connector_id)
    project_acl_grants(migrated, connector_id)
    return migrated


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------


def test_a_role_restricted_project_is_invisible_to_a_non_member(
    synced: Connection, connector_id: UUID
) -> None:
    """Carol is in jira-support, not in the Developers role on ACME."""
    seen = visible_text(synced, principal(synced, connector_id, "u-carol"))

    assert PUB_SUMMARY in seen, "she is named directly on the public project"
    assert ACME_SUMMARY not in seen
    assert ACME_COMMENT not in seen


def test_a_role_restricted_project_is_visible_to_role_members(
    synced: Connection, connector_id: UUID
) -> None:
    """Alice and Bob reach it through jira-developers, not by being named."""
    for account in ("u-alice", "u-bob"):
        seen = visible_text(synced, principal(synced, connector_id, account))
        assert ACME_SUMMARY in seen, f"{account} is in the granted role"
        assert ACME_COMMENT in seen


def test_a_grant_reaches_comments_two_containers_down(
    synced: Connection, connector_id: UUID
) -> None:
    """Project to issue to comment. Slack only ever needed one hop, so this is
    the first real exercise of the recursive walk."""
    alice = visible_text(synced, principal(synced, connector_id, "u-alice"))
    carol = visible_text(synced, principal(synced, connector_id, "u-carol"))

    assert ACME_COMMENT in alice
    assert ACME_COMMENT not in carol
    assert PUB_COMMENT in carol, "the public project's comments still reach her"


def test_group_membership_is_what_grants_access(synced: Connection, connector_id: UUID) -> None:
    """Removing Alice from jira-developers must take ACME away, without any ACL
    changing: the grant is on the group."""
    alice = principal(synced, connector_id, "u-alice")
    assert ACME_SUMMARY in visible_text(synced, alice)

    synced.execute("DELETE FROM principal_memberships WHERE member_id = %s", (alice,))

    assert ACME_SUMMARY not in visible_text(synced, alice)


def test_a_direct_user_actor_grants_without_a_group(synced: Connection, connector_id: UUID) -> None:
    """Carol reaches PUB by being named in a role, not through a group."""
    carol = principal(synced, connector_id, "u-carol")
    synced.execute("DELETE FROM principal_memberships WHERE member_id = %s", (carol,))

    assert PUB_SUMMARY in visible_text(synced, carol)


def test_every_project_grant_expands_to_its_contents(
    synced: Connection, connector_id: UUID
) -> None:
    with synced.cursor() as cur:
        cur.execute("SELECT count(*) FROM acl_source_grants")
        sources = cur.fetchone()
        cur.execute(
            "SELECT count(*) FROM acl_grants g JOIN entities e ON e.id = g.entity_id "
            "WHERE e.entity_type = 'comment'"
        )
        comments = cur.fetchone()

    assert sources is not None
    assert comments is not None
    assert sources[0] == 3, "one grant for ACME, two for PUB"
    assert comments[0] > 0, "grants reached the comments"


def test_revoking_a_role_actor_removes_access(synced: Connection, connector_id: UUID) -> None:
    """The ACL fast-lane's job, one layer down: the source grant goes, the
    projection rebuilds, the content disappears."""
    alice = principal(synced, connector_id, "u-alice")
    assert ACME_SUMMARY in visible_text(synced, alice)

    synced.execute("DELETE FROM acl_source_grants WHERE target_source_id = 'ACME'")
    project_acl_grants(synced, connector_id)

    assert ACME_SUMMARY not in visible_text(synced, alice)
    assert PUB_SUMMARY in visible_text(synced, alice)


def test_syncing_jira_twice_changes_nothing(migrated: Connection, connector_id: UUID) -> None:
    runtime = SyncRuntime(JiraConnector(FixtureTransport(JIRA_FIXTURES)), connector_id)
    runtime.sync_all(migrated)
    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_records")
        before = cur.fetchone()

    SyncRuntime(JiraConnector(FixtureTransport(JIRA_FIXTURES)), connector_id).sync_all(migrated)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM raw_records")
        after = cur.fetchone()
    assert before == after
