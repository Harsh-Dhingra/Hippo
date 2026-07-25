"""Named attack vectors against the permission filter.

The property suite in test_permission_property.py proves the filter agrees with
its specification across random worlds. This file pins the specific ways a
filter like this goes wrong, so that a regression names itself instead of
appearing as one failing random seed.
"""

from uuid import UUID

import pytest
from psycopg import errors

from core.db import Connection

pytestmark = pytest.mark.requires_db

ORG = UUID("00000000-0000-0000-0000-000000000001")

ALICE = UUID("a0000000-0000-0000-0000-00000000000a")
BOB = UUID("b0000000-0000-0000-0000-00000000000b")
CAROL = UUID("c0000000-0000-0000-0000-00000000000c")
ENG = UUID("e0000000-0000-0000-0000-00000000000e")
LEADS = UUID("11000000-0000-0000-0000-000000000011")

E_ENG = UUID("21000000-0000-0000-0000-000000000021")  # granted to the eng group
E_BOB = UUID("22000000-0000-0000-0000-000000000022")  # granted to bob alone
E_NONE = UUID("23000000-0000-0000-0000-000000000023")  # granted to nobody
E_LEADS = UUID("24000000-0000-0000-0000-000000000024")  # granted to the leads group

TEAM_ENG = UUID("31000000-0000-0000-0000-000000000031")
PERSONAL_BOB = UUID("32000000-0000-0000-0000-000000000032")
PERSONAL_ALICE = UUID("33000000-0000-0000-0000-000000000033")

C_ENG_ORG = UUID("41000000-0000-0000-0000-000000000041")
C_BOB_ORG = UUID("42000000-0000-0000-0000-000000000042")
C_NONE_ORG = UUID("43000000-0000-0000-0000-000000000043")
C_ENG_TEAM = UUID("44000000-0000-0000-0000-000000000044")
C_ENG_PERSONAL_BOB = UUID("45000000-0000-0000-0000-000000000045")
C_ENG_PERSONAL_ALICE = UUID("46000000-0000-0000-0000-000000000046")
C_LEADS_ORG = UUID("47000000-0000-0000-0000-000000000047")


def one_hot(index: int) -> str:
    """A unit vector, so cosine distance between two of them is well defined."""
    values = ["0"] * 1024
    values[index] = "1"
    return "[" + ",".join(values) + "]"


@pytest.fixture
def world(migrated: Connection) -> Connection:
    """Alice is in eng; eng is nested inside leads. Bob and Carol are alone."""
    with migrated.cursor() as cur:
        cur.executemany(
            "INSERT INTO principals (id, kind) VALUES (%s, %s)",
            [
                (ALICE, "user"),
                (BOB, "user"),
                (CAROL, "user"),
                (ENG, "group"),
                (LEADS, "group"),
            ],
        )
        cur.executemany(
            "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s)",
            [(ENG, ALICE), (LEADS, ENG)],
        )
        cur.executemany(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'ticket', %s)",
            [(E_ENG, "ENG-1"), (E_BOB, "BOB-1"), (E_NONE, "ORPHAN-1"), (E_LEADS, "LEAD-1")],
        )
        cur.executemany(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
            [(E_ENG, ENG), (E_BOB, BOB), (E_LEADS, LEADS)],
        )
        cur.executemany(
            "INSERT INTO memory_scopes (id, scope_type, owner_principal, owner_kind, name) "
            "VALUES (%s, %s, %s, %s, 'x')",
            [
                (TEAM_ENG, "team", ENG, "group"),
                (PERSONAL_BOB, "personal", BOB, "user"),
                (PERSONAL_ALICE, "personal", ALICE, "user"),
            ],
        )
        cur.executemany(
            "INSERT INTO chunks (id, entity_id, scope_id, content) VALUES (%s, %s, %s, %s)",
            [
                (C_ENG_ORG, E_ENG, ORG, "the renewal is blocked on pricing"),
                (C_BOB_ORG, E_BOB, ORG, "bob private note"),
                (C_NONE_ORG, E_NONE, ORG, "ungranted content"),
                (C_ENG_TEAM, E_ENG, TEAM_ENG, "eng team memo"),
                (C_ENG_PERSONAL_BOB, E_ENG, PERSONAL_BOB, "in bobs personal scope"),
                (C_ENG_PERSONAL_ALICE, E_ENG, PERSONAL_ALICE, "in alices personal scope"),
                (C_LEADS_ORG, E_LEADS, ORG, "leadership planning"),
            ],
        )
    return migrated


def visible(conn: Connection, principal: UUID, **kwargs: object) -> set[UUID]:
    params: dict[str, object] = {
        "text": None,
        "embedding": None,
        "k": 1000,
        "hops": 0,
    }
    params.update(kwargs)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT chunk_id FROM visible_chunks(%s, %s, %s, %s, %s)",
            (principal, params["text"], params["embedding"], params["k"], params["hops"]),
        )
        return {row[0] for row in cur.fetchall()}


# ---------------------------------------------------------------------------
# The ACL half.
# ---------------------------------------------------------------------------


def test_an_entity_with_no_grant_is_invisible_to_everyone(world: Connection) -> None:
    for principal in (ALICE, BOB, CAROL, ENG, LEADS):
        assert C_NONE_ORG not in visible(world, principal)


def test_group_membership_grants_access(world: Connection) -> None:
    assert C_ENG_ORG in visible(world, ALICE)


def test_nested_group_membership_grants_access(world: Connection) -> None:
    """Alice is in eng, eng is in leads, so alice inherits the leads grant."""
    assert C_LEADS_ORG in visible(world, ALICE)


def test_membership_does_not_flow_downwards(world: Connection) -> None:
    """Being a member of eng does not make bob's private grant visible."""
    assert C_BOB_ORG not in visible(world, ALICE)


def test_a_principal_in_no_group_sees_only_its_own_grants(world: Connection) -> None:
    assert visible(world, CAROL) == set()


def test_revoking_a_grant_removes_visibility(world: Connection) -> None:
    assert C_ENG_ORG in visible(world, ALICE)

    world.execute("DELETE FROM acl_grants WHERE entity_id = %s AND principal_id = %s", (E_ENG, ENG))

    assert C_ENG_ORG not in visible(world, ALICE)


def test_removing_a_membership_removes_visibility(world: Connection) -> None:
    """The ACL fast-lane in P1-SYNC-4 depends on this being immediate."""
    assert C_LEADS_ORG in visible(world, ALICE)

    world.execute("DELETE FROM principal_memberships WHERE group_id = %s", (LEADS,))

    assert C_LEADS_ORG not in visible(world, ALICE)


def test_a_membership_cycle_terminates(world: Connection) -> None:
    """Bad data must not hang the filter."""
    world.execute(
        "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s)", (ENG, LEADS)
    )

    assert C_ENG_ORG in visible(world, ALICE)
    assert C_BOB_ORG not in visible(world, ALICE)


# ---------------------------------------------------------------------------
# The scope half. An ACL grant is necessary and not sufficient.
# ---------------------------------------------------------------------------


def test_personal_scope_of_another_user_is_invisible_despite_an_acl_grant(
    world: Connection,
) -> None:
    """Alice can see entity E_ENG, but not the chunk of it that lives in bob's
    personal scope. This is the case a one-sided filter gets wrong."""
    assert C_ENG_ORG in visible(world, ALICE)
    assert C_ENG_PERSONAL_BOB not in visible(world, ALICE)


def test_own_personal_scope_is_visible(world: Connection) -> None:
    assert C_ENG_PERSONAL_ALICE in visible(world, ALICE)


def test_team_scope_is_visible_to_a_member(world: Connection) -> None:
    assert C_ENG_TEAM in visible(world, ALICE)


def test_team_scope_is_invisible_to_a_non_member(world: Connection) -> None:
    world.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (E_ENG, CAROL),
    )

    assert C_ENG_ORG in visible(world, CAROL), "carol now holds the ACL grant"
    assert C_ENG_TEAM not in visible(world, CAROL), "but she is not in the owning group"


def test_org_scope_is_visible_to_anyone_holding_the_grant(world: Connection) -> None:
    assert C_ENG_ORG in visible(world, ALICE)


def test_alice_sees_exactly_the_expected_set(world: Connection) -> None:
    assert visible(world, ALICE) == {
        C_ENG_ORG,
        C_ENG_TEAM,
        C_ENG_PERSONAL_ALICE,
        C_LEADS_ORG,
    }


def test_bob_sees_exactly_the_expected_set(world: Connection) -> None:
    assert visible(world, BOB) == {C_BOB_ORG}


# ---------------------------------------------------------------------------
# Graph expansion cannot be used as a side door.
# ---------------------------------------------------------------------------


def test_expansion_does_not_surface_an_invisible_neighbour(world: Connection) -> None:
    """E_ENG is visible to alice and is one edge from E_BOB, which is not."""
    world.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'mentions', 'source')",
        (E_ENG, E_BOB),
    )

    reached = visible(world, ALICE, text="renewal", hops=2)

    assert C_ENG_ORG in reached
    assert C_BOB_ORG not in reached


def test_expansion_cannot_step_through_an_invisible_entity(world: Connection) -> None:
    """E_ENG -> E_NONE -> E_LEADS. The middle hop is invisible to carol, and the
    walk must not pass through it to reach anything."""
    with world.cursor() as cur:
        cur.executemany(
            "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
            "VALUES (%s, %s, 'mentions', 'source')",
            [(E_ENG, E_NONE), (E_NONE, E_LEADS)],
        )
    world.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (E_ENG, CAROL),
    )

    reached = visible(world, CAROL, text="renewal", hops=2)

    assert reached == {C_ENG_ORG}, "only the seed itself is both reachable and visible"


def test_expansion_reaches_a_visible_neighbour(world: Connection) -> None:
    """The positive case, so the tests above are not passing because expansion
    is simply broken."""
    world.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'blocks', 'source')",
        (E_ENG, E_LEADS),
    )

    reached = visible(world, ALICE, text="renewal", hops=1)

    assert C_ENG_ORG in reached
    assert C_LEADS_ORG in reached


def test_expansion_respects_the_scope_half_too(world: Connection) -> None:
    world.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'blocks', 'source')",
        (E_LEADS, E_ENG),
    )

    reached = visible(world, ALICE, text="leadership", hops=2)

    assert C_ENG_PERSONAL_BOB not in reached


def test_hops_are_clamped(world: Connection) -> None:
    """An out-of-range hop count is clamped, not honoured or rejected."""
    world.execute(
        "INSERT INTO edges (src_id, dst_id, edge_type, provenance) "
        "VALUES (%s, %s, 'blocks', 'source')",
        (E_ENG, E_LEADS),
    )

    assert visible(world, ALICE, text="renewal", hops=99) == visible(
        world, ALICE, text="renewal", hops=2
    )
    assert visible(world, ALICE, text="renewal", hops=-5) == visible(
        world, ALICE, text="renewal", hops=0
    )


# ---------------------------------------------------------------------------
# Every retrieval mode is filtered identically.
# ---------------------------------------------------------------------------


def test_keyword_search_is_filtered(world: Connection) -> None:
    """'note' appears only in bob's chunk. Alice searching for it finds nothing."""
    assert visible(world, BOB, text="note") == {C_BOB_ORG}
    assert visible(world, ALICE, text="note") == set()


def test_vector_search_is_filtered(world: Connection) -> None:
    world.execute("UPDATE chunks SET embedding = %s WHERE id = %s", (one_hot(0), C_BOB_ORG))
    world.execute("UPDATE chunks SET embedding = %s WHERE id = %s", (one_hot(1), C_ENG_ORG))

    assert visible(world, BOB, embedding=one_hot(0)) == {C_BOB_ORG}
    assert visible(world, ALICE, embedding=one_hot(0)) == {C_ENG_ORG}, (
        "alice gets her own nearest match, never bob's"
    )


def test_browse_mode_returns_every_visible_chunk(world: Connection) -> None:
    """Callers with no query still go through the filter, not around it."""
    assert visible(world, BOB) == {C_BOB_ORG}


def test_retrieval_mode_is_reported(world: Connection) -> None:
    with world.cursor() as cur:
        cur.execute("SELECT retrieval_modes FROM visible_chunks(%s, 'renewal', NULL, 10)", (ALICE,))
        assert cur.fetchone() == (["fts"],)


def test_k_limits_the_result_count_not_the_visible_set(world: Connection) -> None:
    everything = visible(world, ALICE)
    assert len(everything) == 4

    limited = visible(world, ALICE, k=2)
    assert len(limited) == 2
    assert limited <= everything


def test_k_is_clamped_to_at_least_one(world: Connection) -> None:
    assert len(visible(world, ALICE, k=0)) == 1
    assert len(visible(world, ALICE, k=-10)) == 1


# ---------------------------------------------------------------------------
# The grant surface around the filter.
# ---------------------------------------------------------------------------


def test_agent_role_can_execute_the_filter_and_gets_filtered_results(world: Connection) -> None:
    """The whole design in one test: the role that can read no table can still
    answer a question, and only sees what it should."""
    with world.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        cur.execute("SELECT chunk_id FROM visible_chunks(%s, NULL, NULL, 1000)", (BOB,))
        seen = {row[0] for row in cur.fetchall()}
        cur.execute("RESET ROLE")

    assert seen == {C_BOB_ORG}


@pytest.mark.parametrize(
    "call",
    [
        "SELECT * FROM _expanded_principals(gen_random_uuid())",
        "SELECT * FROM _visible_entity_ids(gen_random_uuid())",
        "SELECT * FROM _visible_scope_ids(gen_random_uuid())",
    ],
)
def test_agent_role_cannot_execute_the_internal_predicates(migrated: Connection, call: str) -> None:
    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute(call)
    migrated.execute("RESET ROLE")


@pytest.mark.parametrize(
    "function",
    ["_expanded_principals", "_visible_entity_ids", "_visible_scope_ids", "visible_chunks"],
)
def test_public_cannot_execute_any_filter_function(migrated: Connection, function: str) -> None:
    """CREATE FUNCTION grants EXECUTE to PUBLIC by default. It must be revoked."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT bool_or(has_function_privilege('public', p.oid, 'EXECUTE')) "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = %s",
            (function,),
        )
        assert cur.fetchone() == (False,)


@pytest.mark.parametrize(
    "function",
    ["_expanded_principals", "_visible_entity_ids", "_visible_scope_ids", "visible_chunks"],
)
def test_every_filter_function_pins_its_search_path(migrated: Connection, function: str) -> None:
    """An unpinned search_path on a SECURITY DEFINER function is an escalation
    hole, and the invoker functions are called from inside one."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT p.proconfig FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = %s",
            (function,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] is not None, f"{function} does not pin search_path"
    assert any(setting.startswith("search_path=") for setting in row[0])


def test_only_the_entry_point_is_security_definer(migrated: Connection) -> None:
    """The helpers run as the caller, so a leaked EXECUTE grant on one of them
    would still return nothing to a role that cannot read the tables."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.prosecdef FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname IN "
            "('_expanded_principals','_visible_entity_ids','_visible_scope_ids','visible_chunks') "
            "ORDER BY p.proname"
        )
        assert cur.fetchall() == [
            ("_expanded_principals", False),
            ("_visible_entity_ids", False),
            ("_visible_scope_ids", False),
            ("visible_chunks", True),
        ]


# ---------------------------------------------------------------------------
# Membership integrity that the scope check depends on.
# ---------------------------------------------------------------------------


def test_a_user_cannot_be_used_as_a_group(world: Connection) -> None:
    """Without this, bob could be given 'members' and his personal scope would
    expand to them."""
    with pytest.raises(errors.ForeignKeyViolation):
        world.execute(
            "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s)",
            (BOB, CAROL),
        )
