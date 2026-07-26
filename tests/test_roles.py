"""Role-grant leak test (P1-CORE-2 done-condition).

The permission story is enforced by Postgres grants, not by application code, so
it is asserted the same way: by running as each role and watching the database
refuse. Every assertion here is about what a role *cannot* do.

The grant matrix below is exhaustive on purpose. A migration that adds a table
without adding it here fails `test_grant_matrix_covers_every_table`, so a new
table can never become readable by omission.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

import psycopg
import pytest
from psycopg import errors

from core.db import Connection, connect
from core.migrate import upgrade

pytestmark = pytest.mark.requires_db

ROLES = ("hippo_sync", "hippo_resolver", "hippo_agent", "hippo_api")

READ = frozenset({"SELECT"})
WRITE = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})
NONE: frozenset[str] = frozenset()

# role -> table -> privileges that role is allowed to hold.
EXPECTED_GRANTS: dict[str, dict[str, frozenset[str]]] = {
    "hippo_sync": {
        "connectors": WRITE,
        "sync_state": WRITE,
        "raw_records": WRITE,
        "principals": WRITE,
        "principal_memberships": WRITE,
        "acl_grants": WRITE,
        # Table-level. Migration 017 narrowed UPDATE to specific columns so the
        # executing role cannot also approve; see the column-grant test below,
        # which has_table_privilege cannot see.
        "actions": READ,
        "entities": READ,
        "entity_sources": READ,
        "jobs": WRITE,
        "acl_source_grants": WRITE,
        "edges": NONE,
        "chunks": NONE,
        "memory_scopes": NONE,
        "memory_notes": NONE,
        "schema_migrations": NONE,
        # Traces belong to the agent. Sync has no reason to read what anyone
        # asked, and giving it one would make the trace log a second place to
        # go looking for content.
        "query_traces": NONE,
        "trace_retrievals": NONE,
        "users": NONE,
        "sessions": NONE,
        "action_events": NONE,
        "alerts": NONE,
    },
    "hippo_resolver": {
        "raw_records": READ,
        "connectors": READ,
        "principals": READ,
        "principal_memberships": READ,
        "entities": WRITE,
        "entity_sources": WRITE,
        "edges": WRITE,
        "chunks": WRITE,
        "memory_scopes": READ,
        "jobs": WRITE,
        "acl_source_grants": READ,
        "acl_grants": NONE,
        "sync_state": NONE,
        "actions": NONE,
        "memory_notes": NONE,
        "schema_migrations": NONE,
        "query_traces": NONE,
        "trace_retrievals": NONE,
        "users": NONE,
        "sessions": NONE,
        "action_events": NONE,
        "alerts": NONE,
    },
    "hippo_agent": {
        "actions": frozenset({"INSERT"}),
        # Write freely, read only your own — and reading goes through
        # my_trace()/my_traces(), never a SELECT.
        "query_traces": frozenset({"INSERT"}),
        "trace_retrievals": frozenset({"INSERT"}),
        # The agent proposes into actions. It does not schedule work.
        "jobs": NONE,
        "acl_source_grants": NONE,
        "chunks": NONE,
        "raw_records": NONE,
        "entities": NONE,
        "entity_sources": NONE,
        "edges": NONE,
        "acl_grants": NONE,
        "principals": NONE,
        "principal_memberships": NONE,
        "connectors": NONE,
        "sync_state": NONE,
        "memory_scopes": NONE,
        "memory_notes": NONE,
        "schema_migrations": NONE,
        "users": NONE,
        "sessions": NONE,
        "action_events": NONE,
        "alerts": NONE,
    },
    # Serves people: logins, approvals, and enough to render one. It reads no
    # content — a query runs under SET LOCAL ROLE hippo_agent, so a prompt
    # still reaches chunks only through visible_chunks().
    "hippo_api": {
        "users": WRITE,
        "sessions": WRITE,
        # Approve and decline. Not INSERT: a proposal comes from the agent, and
        # an API that could mint its own would make the split decorative.
        "actions": frozenset({"SELECT", "UPDATE"}),
        "connectors": READ,
        "principals": READ,
        # Revoked by 013. An entity title is content — a Jira issue's title is
        # its summary — so reading one here would be a path around
        # visible_chunks() held by the process that serves users. The approval
        # screen reads actions.summary instead.
        "entities": NONE,
        "chunks": NONE,
        "raw_records": NONE,
        "entity_sources": NONE,
        "edges": NONE,
        "acl_grants": NONE,
        "acl_source_grants": NONE,
        "principal_memberships": NONE,
        "sync_state": NONE,
        "memory_scopes": NONE,
        # Notes are the one place a person writes into memory directly, so the
        # role serving people owns the table. Reading them back as retrievable
        # memory still goes through visible_chunks like everything else.
        "memory_notes": WRITE,
        "jobs": NONE,
        "schema_migrations": NONE,
        # Read through my_trace()/my_traces(), never a SELECT — the same shape
        # the agent writes them with.
        "query_traces": NONE,
        "trace_retrievals": NONE,
        # Append-only. The trigger writes it as the owner; no service role holds
        # INSERT, UPDATE or DELETE, because a log the application can rewrite
        # answers "what do we currently claim happened".
        "action_events": NONE,
        # Read to show them, and a column grant for notified_at so delivery can
        # be recorded. Raising one goes through a granted function.
        "alerts": READ,
    },
}

# Tables holding synced or derived content. No role may read these except
# through the permission filter, and the agent may not read them at all.
CONTENT_TABLES = ("chunks", "raw_records", "entities", "edges", "memory_notes")


@contextmanager
def as_role(conn: Connection, role: str) -> Iterator[None]:
    """Run the enclosed statements with the privileges of `role`."""
    with conn.cursor() as cur:
        cur.execute(f'SET ROLE "{role}"')
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("RESET ROLE")


@pytest.fixture
def seeded(migrated: Connection) -> Connection:
    """Enough rows for a foreign-key-valid action insert."""
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) "
            "VALUES ('11111111-1111-1111-1111-111111111111', 'jira', 'Jira')"
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id, email) VALUES "
            "('22222222-2222-2222-2222-222222222222', 'user', "
            "'11111111-1111-1111-1111-111111111111', 'u1', 'someone@example.com')"
        )
    return migrated


def _actual_privileges(conn: Connection, role: str, table: str) -> frozenset[str]:
    held: set[str] = set()
    with conn.cursor() as cur:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            cur.execute("SELECT has_table_privilege(%s, %s, %s)", (role, table, privilege))
            row = cur.fetchone()
            if row is not None and row[0]:
                held.add(privilege)
    return frozenset(held)


# ---------------------------------------------------------------------------
# The done-condition: the agent role cannot read chunks.
# ---------------------------------------------------------------------------


def test_agent_role_cannot_select_chunks(migrated: Connection) -> None:
    """P1-CORE-2's done-condition, stated as the query that must fail."""
    with as_role(migrated, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute("SELECT content FROM chunks LIMIT 1")


@pytest.mark.parametrize("table", CONTENT_TABLES)
def test_agent_role_cannot_select_any_content_table(migrated: Connection, table: str) -> None:
    with as_role(migrated, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute(f"SELECT * FROM {table} LIMIT 1")


def test_agent_role_cannot_reach_chunks_through_a_join(migrated: Connection) -> None:
    """Reading chunks via a table the role can touch is still reading chunks."""
    with as_role(migrated, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute(
            "SELECT c.content FROM actions a JOIN chunks c ON c.entity_id = a.target_entity"
        )


def test_agent_role_cannot_select_acl_grants(migrated: Connection) -> None:
    """Who-can-see-what is not the agent's to read either."""
    with as_role(migrated, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute("SELECT * FROM acl_grants LIMIT 1")


# ---------------------------------------------------------------------------
# The agent proposes, and only proposes.
# ---------------------------------------------------------------------------


def test_agent_role_can_insert_a_pending_action(seeded: Connection) -> None:
    action_id = uuid4()
    with as_role(seeded, "hippo_agent"):
        seeded.execute(
            "INSERT INTO actions (id, requested_by, connector_id, action_type, payload, "
            "risk_class, status) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                action_id,
                "22222222-2222-2222-2222-222222222222",
                "11111111-1111-1111-1111-111111111111",
                "jira.comment",
                "{}",
                "consequential",
                "pending",
            ),
        )

    with seeded.cursor() as cur:
        cur.execute("SELECT status FROM actions WHERE id = %s", (action_id,))
        assert cur.fetchone() == ("pending",)


def test_agent_role_cannot_read_back_what_it_inserted(seeded: Connection) -> None:
    """INSERT ... RETURNING needs SELECT. The agent generates ids client-side."""
    with as_role(seeded, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        seeded.execute(
            "INSERT INTO actions (requested_by, connector_id, action_type, payload, "
            "risk_class) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (
                "22222222-2222-2222-2222-222222222222",
                "11111111-1111-1111-1111-111111111111",
                "jira.comment",
                "{}",
                "consequential",
            ),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE actions SET status = 'approved'",
        "UPDATE actions SET status = 'executed'",
        "DELETE FROM actions",
    ],
)
def test_agent_role_cannot_approve_or_execute_its_own_proposals(
    migrated: Connection, statement: str
) -> None:
    with as_role(migrated, "hippo_agent"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute(statement)


# ---------------------------------------------------------------------------
# The other two roles stay inside their lane.
# ---------------------------------------------------------------------------


def test_sync_role_cannot_write_the_graph(migrated: Connection) -> None:
    """Sync never interprets content into entities (ARCHITECTURE §2)."""
    with as_role(migrated, "hippo_sync"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute("INSERT INTO entities (entity_type) VALUES ('person')")


def test_sync_role_cannot_create_actions(migrated: Connection) -> None:
    """Proposing is the agent's job; sync only executes what a human approved."""
    with as_role(migrated, "hippo_sync"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute(
            "INSERT INTO actions (requested_by, connector_id, action_type, payload, risk_class) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'jira.comment', '{}', 'routine')"
        )


def test_resolver_role_cannot_write_acl_grants(migrated: Connection) -> None:
    """A resolver that could write ACLs could grant itself access."""
    with as_role(migrated, "hippo_resolver"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) "
            "VALUES (gen_random_uuid(), gen_random_uuid(), 'slack')"
        )


def test_resolver_role_cannot_mutate_raw_records(migrated: Connection) -> None:
    """CLAUDE.md rule 4: raw records are immutable source truth."""
    with as_role(migrated, "hippo_resolver"), pytest.raises(errors.InsufficientPrivilege):
        migrated.execute("UPDATE raw_records SET payload = '{}'")


def test_resolver_role_can_write_the_graph(migrated: Connection) -> None:
    with as_role(migrated, "hippo_resolver"):
        migrated.execute("INSERT INTO entities (entity_type, title) VALUES ('person', 'Ada')")

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM entities")
        assert cur.fetchone() == (1,)


def test_no_service_role_can_touch_the_migration_ledger(migrated: Connection) -> None:
    for role in ROLES:
        with as_role(migrated, role), pytest.raises(errors.InsufficientPrivilege):
            migrated.execute("SELECT * FROM schema_migrations")


def test_no_service_role_can_create_objects(migrated: Connection) -> None:
    for role in ROLES:
        with as_role(migrated, role), pytest.raises(errors.InsufficientPrivilege):
            migrated.execute("CREATE TABLE sneaky (id int)")


# ---------------------------------------------------------------------------
# The matrix, checked exhaustively.
# ---------------------------------------------------------------------------


def test_grant_matrix_covers_every_table(migrated: Connection) -> None:
    """A new table must be an explicit decision, never a default."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )
        actual = {row[0] for row in cur.fetchall()}

    for role, matrix in EXPECTED_GRANTS.items():
        assert set(matrix) == actual, (
            f"{role}'s grant matrix does not match the schema. "
            f"Missing from matrix: {sorted(actual - set(matrix))}. "
            f"Not in schema: {sorted(set(matrix) - actual)}."
        )


@pytest.mark.parametrize("role", ROLES)
def test_privileges_match_the_matrix_exactly(migrated: Connection, role: str) -> None:
    mismatches = {
        table: (expected, _actual_privileges(migrated, role, table))
        for table, expected in EXPECTED_GRANTS[role].items()
        if _actual_privileges(migrated, role, table) != expected
    }
    assert mismatches == {}, f"{role} holds privileges the matrix does not allow: {mismatches}"


@pytest.mark.parametrize("role", ROLES)
def test_roles_cannot_log_in_directly(migrated: Connection, role: str) -> None:
    """Group roles only. Login users are the operator's to create, with secrets
    that never appear in a migration."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, rolcreaterole "
            "FROM pg_roles WHERE rolname = %s",
            (role,),
        )
        assert cur.fetchone() == (False, False, False, False, False)


def test_migration_is_idempotent_across_databases(db_dsn: str, migrated: Connection) -> None:
    """Roles are cluster-wide, so a second database must not fail to create them."""
    with connect(db_dsn, autocommit=True) as conn:
        assert upgrade(conn) == ()


def test_roles_migration_is_deliberately_irreversible() -> None:
    """Roles outlive the database that created them, so dropping them from one
    database's downgrade could break another database in the same cluster."""
    from core.migrate import discover

    roles_migration = next(m for m in discover() if m.name == "roles")
    assert roles_migration.reversible is False


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("hippo_agent", ["my_trace", "my_traces", "timeline", "visible_chunks"]),
        ("hippo_sync", ["project_acl_grants", "raise_alert"]),
        ("hippo_resolver", []),
        # The API serves the trace view and the approval buttons. It never
        # calls visible_chunks(): a query runs as hippo_agent instead.
        (
            "hippo_api",
            [
                "acknowledge_alert",
                "ensure_personal_scope",
                "my_action_events",
                "my_notes",
                "my_principals",
                "my_scopes",
                "my_trace",
                "my_traces",
                "open_alerts",
                "project_note",
                "raise_alert",
                "timeline",
                "unproject_note",
            ],
        ),
    ],
)
def test_function_surface_is_exactly_what_was_granted(
    migrated: Connection, role: str, expected: list[str]
) -> None:
    """The agent's entire read path is one function. Extension-owned functions
    are excluded: pgvector and pgcrypto expose pure computation with no data
    access, and they are PUBLIC-executable by design."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT p.proname "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' "
            "  AND NOT EXISTS (SELECT 1 FROM pg_depend d "
            "                   WHERE d.objid = p.oid AND d.deptype = 'e') "
            "  AND has_function_privilege(%s, p.oid, 'EXECUTE') "
            "ORDER BY p.proname",
            (role,),
        )
        assert [row[0] for row in cur.fetchall()] == expected


def test_owner_retains_full_access(migrated: Connection) -> None:
    """Sanity check on the harness itself: the assertions above must be failing
    because of grants, not because the queries are broken."""
    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks")
        assert cur.fetchone() == (0,)


def test_setting_role_actually_drops_privileges(migrated: Connection) -> None:
    """If SET ROLE did not take effect, every negative test above would pass
    vacuously. Prove the mechanism works before trusting it."""
    with as_role(migrated, "hippo_agent"), migrated.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone() == ("hippo_agent",)

    with migrated.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone() != ("hippo_agent",)


def test_content_tables_are_unreadable_without_a_grant(migrated: Connection) -> None:
    """Nothing is world-readable: PUBLIC holds no privileges on content."""
    with migrated.cursor() as cur:
        for table in CONTENT_TABLES:
            cur.execute("SELECT has_table_privilege('public', %s, 'SELECT')", (table,))
            row = cur.fetchone()
            assert row is not None
            assert row[0] is False, f"{table} is readable by PUBLIC"


def test_psycopg_reports_privilege_errors_as_expected(migrated: Connection) -> None:
    """Guards the negative tests against silently catching the wrong error."""
    with as_role(migrated, "hippo_agent"), pytest.raises(psycopg.Error) as caught:
        migrated.execute("SELECT * FROM chunks")
    assert caught.value.sqlstate == "42501"


# ---------------------------------------------------------------------------
# Column-level grants, which has_table_privilege above cannot see.
# ---------------------------------------------------------------------------

# What the executing role may write on an action. Everything here is a fact
# about an execution that already happened; nothing here is a decision to let
# one happen. That split is what stops propose, approve and execute collapsing
# into one component (migration 017).
SYNC_MAY_UPDATE = frozenset(
    {
        "status",
        "executed_at",
        "inverse_payload",
        "receipt",
        "error",
        "execution_started_at",
        "rolled_back_at",
        "rolled_back_by",
    }
)


def column_grants(conn: Connection, role: str, table: str, privilege: str) -> frozenset[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.column_privileges "
            "WHERE grantee = %s AND table_name = %s AND privilege_type = %s",
            (role, table, privilege),
        )
        return frozenset(str(row[0]) for row in cur.fetchall())


def test_the_executing_role_may_only_write_execution_columns(migrated: Connection) -> None:
    """The boundary migration 017 introduced, pinned exactly.

    A table-level grant would have been invisible to this file's other tests,
    because has_table_privilege reports nothing about columns. This is what
    stops a future migration quietly handing the worker back the whole row.
    """
    granted = column_grants(migrated, "hippo_sync", "actions", "UPDATE")

    assert granted == SYNC_MAY_UPDATE


def test_the_executing_role_cannot_write_either_approval_column(migrated: Connection) -> None:
    """Stated separately from the set above, because these two columns are the
    entire point of the split and deserve to fail with their own name."""
    granted = column_grants(migrated, "hippo_sync", "actions", "UPDATE")

    assert "approved_by" not in granted
    assert "approved_by_policy" not in granted


def test_the_role_that_represents_people_may_approve(migrated: Connection) -> None:
    """The other half. hippo_api holds table-level UPDATE and no source-system
    credential, so what decides cannot be what acts."""
    with migrated.cursor() as cur:
        cur.execute("SELECT has_table_privilege('hippo_api', 'actions', 'UPDATE')")
        assert cur.fetchone() == (True,)


def test_no_role_holds_an_unexpected_column_grant(migrated: Connection) -> None:
    """Column grants are the shape of privilege this file was previously blind
    to, so they get their own sweep rather than a single assertion."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT grantee, table_name, privilege_type, count(*) "
            "FROM information_schema.column_privileges "
            "WHERE grantee = ANY(%s) "
            "  AND NOT EXISTS (SELECT 1 FROM information_schema.table_privileges t "
            "                  WHERE t.grantee = column_privileges.grantee "
            "                    AND t.table_name = column_privileges.table_name "
            "                    AND t.privilege_type = column_privileges.privilege_type) "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
            (list(ROLES),),
        )
        column_only = [(str(r[0]), str(r[1]), str(r[2])) for r in cur.fetchall()]

    assert column_only == [
        ("hippo_api", "alerts", "UPDATE"),
        ("hippo_sync", "actions", "UPDATE"),
    ], column_only
