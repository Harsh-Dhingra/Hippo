"""The permission property suite (P1-CORE-3 done-condition).

Random ACL matrices, an independent expectation, zero leaks.

The oracle below is written from the specification in ARCHITECTURE §3 and the
comments in 003_visible_chunks.sql, deliberately not from the SQL. An oracle
derived from the implementation tests only that the implementation equals
itself. If the two ever disagree, one of them is wrong and that is the point.

A "case" is one (matrix, principal) pair: one randomly generated permission
world, evaluated from one principal's point of view. The headline test runs at
least 10,000 of them.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.db import Connection

pytestmark = [pytest.mark.requires_db, pytest.mark.permission_property]

ORG_SCOPE = UUID("00000000-0000-0000-0000-000000000001")

# Tables the generator owns. Truncated between matrices, in dependency order.
_OWNED_TABLES = (
    "chunks",
    "acl_grants",
    "edges",
    "principal_memberships",
    "memory_scopes",
    "entities",
    "principals",
)


@dataclass(frozen=True)
class Scope:
    id: UUID
    scope_type: str
    owner: UUID | None
    owner_kind: str | None


@dataclass(frozen=True)
class Matrix:
    """One randomly generated permission world."""

    users: tuple[UUID, ...]
    groups: tuple[UUID, ...]
    memberships: tuple[tuple[UUID, UUID], ...]  # (group, member)
    entities: tuple[UUID, ...]
    grants: tuple[tuple[UUID, UUID], ...]  # (entity, principal)
    scopes: tuple[Scope, ...]
    chunks: tuple[tuple[UUID, UUID, UUID], ...]  # (chunk, entity, scope)

    @property
    def principals(self) -> tuple[UUID, ...]:
        return self.users + self.groups


# ---------------------------------------------------------------------------
# The oracle, from the spec.
# ---------------------------------------------------------------------------


def expanded_principals(matrix: Matrix, principal: UUID) -> set[UUID]:
    """The principal itself, plus every group that transitively contains it."""
    closure = {principal}
    frontier = [principal]
    while frontier:
        member = frontier.pop()
        for group, contained in matrix.memberships:
            if contained == member and group not in closure:
                closure.add(group)
                frontier.append(group)
    return closure


def expected_visible_chunks(matrix: Matrix, principal: UUID) -> set[UUID]:
    """A chunk is visible when BOTH halves of the predicate hold.

    ACL:   some acl_grants row for the chunk's entity names a principal in the
           closure. No row means invisible; there is no default grant.
    SCOPE: the chunk's scope is the org scope, or its owner is in the closure.
           A personal scope's owner is a user, and a user is in the closure only
           when it is the asking principal.
    """
    closure = expanded_principals(matrix, principal)
    visible_entities = {entity for entity, holder in matrix.grants if holder in closure}
    visible_scopes = {
        scope.id
        for scope in matrix.scopes
        if scope.scope_type == "org" or (scope.owner is not None and scope.owner in closure)
    }
    return {
        chunk
        for chunk, entity, scope in matrix.chunks
        if entity in visible_entities and scope in visible_scopes
    }


# ---------------------------------------------------------------------------
# Generation.
# ---------------------------------------------------------------------------


def random_matrix(rng: random.Random) -> Matrix:
    """A small permission world, biased towards the shapes that break filters:
    nested groups, unreferenced entities, personal scopes, and grants held by
    groups the asking principal does not belong to."""
    users = tuple(uuid4() for _ in range(rng.randint(2, 6)))
    groups = tuple(uuid4() for _ in range(rng.randint(1, 4)))

    memberships: set[tuple[UUID, UUID]] = set()
    for group in groups:
        for user in users:
            if rng.random() < 0.4:
                memberships.add((group, user))
    # Nested groups: a group inside another group. Cycles are possible and must
    # not hang the closure.
    for outer in groups:
        for inner in groups:
            if outer != inner and rng.random() < 0.25:
                memberships.add((outer, inner))

    entities = tuple(uuid4() for _ in range(rng.randint(2, 6)))
    principals = users + groups
    grants: set[tuple[UUID, UUID]] = set()
    for entity in entities:
        # Some entities deliberately get no grant at all.
        for _ in range(rng.randint(0, 3)):
            grants.add((entity, rng.choice(principals)))

    scopes = [Scope(ORG_SCOPE, "org", None, None)]
    for group in groups:
        if rng.random() < 0.5:
            scopes.append(Scope(uuid4(), "team", group, "group"))
    for user in users:
        if rng.random() < 0.5:
            scopes.append(Scope(uuid4(), "personal", user, "user"))

    chunks = tuple(
        (uuid4(), rng.choice(entities), rng.choice(scopes).id) for _ in range(rng.randint(3, 12))
    )

    return Matrix(
        users=users,
        groups=groups,
        memberships=tuple(sorted(memberships)),
        entities=entities,
        grants=tuple(sorted(grants)),
        scopes=tuple(scopes),
        chunks=chunks,
    )


def load(conn: Connection, matrix: Matrix) -> None:
    """Replace the database contents with this matrix."""
    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE {', '.join(_OWNED_TABLES)} CASCADE")
        cur.executemany(
            "INSERT INTO principals (id, kind) VALUES (%s, %s)",
            [(u, "user") for u in matrix.users] + [(g, "group") for g in matrix.groups],
        )
        if matrix.memberships:
            cur.executemany(
                "INSERT INTO principal_memberships (group_id, member_id) VALUES (%s, %s)",
                list(matrix.memberships),
            )
        cur.executemany(
            "INSERT INTO entities (id, entity_type) VALUES (%s, 'ticket')",
            [(e,) for e in matrix.entities],
        )
        if matrix.grants:
            cur.executemany(
                "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
                list(matrix.grants),
            )
        cur.executemany(
            "INSERT INTO memory_scopes (id, scope_type, owner_principal, owner_kind, name) "
            "VALUES (%s, %s, %s, %s, 'generated')",
            [(s.id, s.scope_type, s.owner, s.owner_kind) for s in matrix.scopes],
        )
        # Distinct text per chunk: chunks are content-addressed within an
        # entity, so two chunks of one entity saying the same thing are one
        # chunk. Generating identical bodies would be generating a world the
        # schema does not permit.
        cur.executemany(
            "INSERT INTO chunks (id, entity_id, scope_id, content) VALUES (%s, %s, %s, %s)",
            [(chunk, entity, scope, str(chunk)) for chunk, entity, scope in matrix.chunks],
        )


def actual_visible(conn: Connection, matrix: Matrix) -> dict[UUID, set[UUID]]:
    """What the filter says every principal can see, in one round trip."""
    seen: dict[UUID, set[UUID]] = {principal: set() for principal in matrix.principals}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT p.id, vc.chunk_id "
            "FROM principals p "
            "CROSS JOIN LATERAL visible_chunks(p.id, NULL, NULL, 100000, 0) vc"
        )
        for principal, chunk in cur.fetchall():
            seen[principal].add(chunk)
    return seen


def _describe(matrix: Matrix, principal: UUID, leaked: set[UUID], missing: set[UUID]) -> str:
    return (
        f"principal={principal}\n"
        f"leaked={sorted(leaked)}\n"
        f"missing={sorted(missing)}\n"
        f"memberships={matrix.memberships}\n"
        f"grants={matrix.grants}\n"
        f"scopes={matrix.scopes}\n"
        f"chunks={matrix.chunks}"
    )


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------

REQUIRED_CASES = 10_000


def test_ten_thousand_random_acl_matrices_leak_nothing(migrated: Connection) -> None:
    """P1-CORE-3's done-condition. Seeded, so a failure reproduces exactly."""
    rng = random.Random(20260724)
    cases = 0

    while cases < REQUIRED_CASES:
        matrix = random_matrix(rng)
        load(migrated, matrix)
        observed = actual_visible(migrated, matrix)

        for principal in matrix.principals:
            expected = expected_visible_chunks(matrix, principal)
            got = observed[principal]
            leaked = got - expected
            missing = expected - got

            assert not leaked, "LEAK: " + _describe(matrix, principal, leaked, missing)
            assert not missing, "over-filtered: " + _describe(matrix, principal, leaked, missing)
            cases += 1

    assert cases >= REQUIRED_CASES


def test_an_unknown_principal_sees_nothing(migrated: Connection) -> None:
    """Deny by default, stated as its own case."""
    rng = random.Random(1)
    matrix = random_matrix(rng)
    load(migrated, matrix)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM visible_chunks(%s, NULL, NULL, 100000)", (uuid4(),))
        assert cur.fetchone() == (0,)


# ---------------------------------------------------------------------------
# Hypothesis, for shrinking. Smaller run; the bulk suite above is the gate.
# ---------------------------------------------------------------------------


@st.composite
def matrices(draw: st.DrawFn) -> Matrix:
    seed = draw(st.integers(min_value=0, max_value=2**32 - 1))
    return random_matrix(random.Random(seed))


@given(matrix=matrices())
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_filter_matches_the_oracle_for_every_principal(
    migrated: Connection, matrix: Matrix
) -> None:
    """Same property, but Hypothesis shrinks a counterexample to something
    small enough to read when it fails."""
    load(migrated, matrix)
    observed = actual_visible(migrated, matrix)

    for principal in matrix.principals:
        expected = expected_visible_chunks(matrix, principal)
        got = observed[principal]
        assert got == expected, _describe(matrix, principal, got - expected, expected - got)
