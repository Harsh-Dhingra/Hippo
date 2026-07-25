"""Passwords, sessions and the user-to-principal join.

The HTTP tests in test_api_v1.py cover the happy paths. These cover the ones a
request cannot easily reach: a corrupt stored hash, an expired session, a user
who signs up before sync has met them.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import pytest

from api import auth
from core.db import Connection

pytestmark = pytest.mark.requires_db

PASSWORD = "correct horse battery staple"


@pytest.fixture
def alice_principal(migrated: Connection) -> UUID:
    connector_id = uuid4()
    principal_id = uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
            (connector_id,),
        )
        cur.execute(
            "INSERT INTO principals (id, kind, connector_id, source_id, email) "
            "VALUES (%s, 'user', %s, 'U-ALICE', 'Alice@Example.com')",
            (principal_id, connector_id),
        )
    return principal_id


# ---------------------------------------------------------------------------
# Passwords.
# ---------------------------------------------------------------------------


def test_a_password_round_trips() -> None:
    encoded = auth.hash_password(PASSWORD)

    assert auth.verify_password(PASSWORD, encoded) is True
    assert auth.verify_password(PASSWORD + " ", encoded) is False


def test_the_same_password_hashes_differently_every_time() -> None:
    """Per-password salt: two people with the same password must not be
    visibly the same in a dump."""
    assert auth.hash_password(PASSWORD) != auth.hash_password(PASSWORD)


def test_the_stored_hash_contains_no_password() -> None:
    encoded = auth.hash_password("hunter2")

    assert "hunter2" not in encoded
    assert encoded.startswith("scrypt$")


def test_the_parameters_travel_with_the_hash() -> None:
    """So raising the cost later verifies every hash written before it."""
    cheap = f"scrypt$16384$8$1${'ab' * 16}$"
    digest = auth._scrypt(PASSWORD, bytes.fromhex("ab" * 16), 16384, 8, 1, 32)

    assert auth.verify_password(PASSWORD, cheap + digest.hex()) is True


@pytest.mark.parametrize(
    "encoded",
    [
        "",
        "not-a-hash",
        "bcrypt$1$2$3$aa$bb",
        "scrypt$notanumber$8$1$aa$bb",
        "scrypt$16384$8$1$zz$bb",
        "scrypt$16384$8$1",
    ],
)
def test_a_corrupt_hash_fails_the_check_instead_of_raising(encoded: str) -> None:
    """A corrupt row should lock one account out, not return a 500 that says
    which account is corrupt."""
    assert auth.verify_password(PASSWORD, encoded) is False


def test_an_empty_password_is_refused_at_the_source() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        auth.hash_password("")


# ---------------------------------------------------------------------------
# Users and principals.
# ---------------------------------------------------------------------------


def test_a_new_user_is_linked_to_their_principal(
    migrated: Connection, alice_principal: UUID
) -> None:
    user = auth.create_user(migrated, "alice@example.com", PASSWORD)

    assert user.principal_id == alice_principal
    assert user.can_see_anything is True


def test_the_link_ignores_case_and_padding(migrated: Connection, alice_principal: UUID) -> None:
    user = auth.create_user(migrated, "  ALICE@Example.com  ", PASSWORD)

    assert user.email == "alice@example.com"
    assert user.principal_id == alice_principal


def test_a_user_with_no_principal_is_a_normal_state(migrated: Connection) -> None:
    """Signing up grants nothing, and someone sync has not met yet is not an
    error."""
    user = auth.create_user(migrated, "stranger@example.com", PASSWORD)

    assert user.principal_id is None
    assert user.can_see_anything is False


def test_relinking_attaches_users_that_sync_has_since_met(migrated: Connection) -> None:
    """Someone who signs up before their Slack account is synced would
    otherwise stay blind forever."""
    user = auth.create_user(migrated, "alice@example.com", PASSWORD)
    assert user.principal_id is None
    connector_id = uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
            (connector_id,),
        )
        cur.execute(
            "INSERT INTO principals (kind, connector_id, source_id, email) "
            "VALUES ('user', %s, 'U-ALICE', 'alice@example.com')",
            (connector_id,),
        )

    assert auth.relink_principals(migrated) == 1

    with migrated.cursor() as cur:
        cur.execute("SELECT principal_id FROM users WHERE email = 'alice@example.com'")
        assert (cur.fetchone() or (None,))[0] is not None


def test_relinking_never_detaches(migrated: Connection, alice_principal: UUID) -> None:
    """A principal disappearing from a source is a revocation question, and
    revocation is the ACL fast-lane's job."""
    auth.create_user(migrated, "alice@example.com", PASSWORD)

    assert auth.relink_principals(migrated) == 0

    with migrated.cursor() as cur:
        cur.execute("SELECT principal_id FROM users WHERE email = 'alice@example.com'")
        assert (cur.fetchone() or (None,))[0] == alice_principal


def test_an_unnormalised_email_cannot_be_written_directly(migrated: Connection) -> None:
    """The CHECK is there because the unique index is on the raw column: two
    rows differing only in case would both be insertable otherwise."""
    from psycopg import errors

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            "INSERT INTO users (email, password_hash) VALUES ('Alice@Example.com', 'x')"
        )
    migrated.rollback()


def test_two_users_cannot_share_an_email(migrated: Connection) -> None:
    from psycopg import errors

    auth.create_user(migrated, "alice@example.com", PASSWORD)

    with pytest.raises(errors.UniqueViolation):
        auth.create_user(migrated, "ALICE@example.com", PASSWORD)
    migrated.rollback()


# ---------------------------------------------------------------------------
# Sessions.
# ---------------------------------------------------------------------------


def test_logging_in_issues_a_token(migrated: Connection, alice_principal: UUID) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)

    session = auth.login(migrated, "alice@example.com", PASSWORD)

    assert session.token
    assert session.user.principal_id == alice_principal


def test_an_unknown_email_and_a_wrong_password_are_the_same_answer(
    migrated: Connection,
) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)

    with pytest.raises(auth.AuthError, match="invalid email or password"):
        auth.login(migrated, "nobody@example.com", PASSWORD)
    with pytest.raises(auth.AuthError, match="invalid email or password"):
        auth.login(migrated, "alice@example.com", "wrong")


def test_a_disabled_account_cannot_log_in(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    migrated.execute("UPDATE users SET disabled_at = now()")

    with pytest.raises(auth.AuthError, match="disabled"):
        auth.login(migrated, "alice@example.com", PASSWORD)


def test_a_token_resolves_to_its_user(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD)

    assert auth.authenticate(migrated, session.token).email == "alice@example.com"


def test_an_unknown_token_is_refused(migrated: Connection) -> None:
    with pytest.raises(auth.AuthError, match="not authenticated"):
        auth.authenticate(migrated, "made up")


def test_an_expired_session_is_refused(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD, ttl=timedelta(seconds=1))
    migrated.execute(
        "UPDATE sessions SET expires_at = now() - interval '1 minute', "
        "created_at = now() - interval '2 minutes'"
    )

    with pytest.raises(auth.AuthError, match="expired"):
        auth.authenticate(migrated, session.token)


def test_a_revoked_session_is_refused(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD)

    assert auth.logout(migrated, session.token) is True

    with pytest.raises(auth.AuthError, match="signed out"):
        auth.authenticate(migrated, session.token)


def test_logging_out_twice_is_harmless(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD)
    auth.logout(migrated, session.token)

    assert auth.logout(migrated, session.token) is False


def test_logging_out_an_unknown_token_is_harmless(migrated: Connection) -> None:
    assert auth.logout(migrated, "made up") is False


def test_disabling_an_account_ends_its_live_sessions(migrated: Connection) -> None:
    """Checked per request, not per login: disabling someone has to take effect
    now."""
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD)
    migrated.execute("UPDATE users SET disabled_at = now()")

    with pytest.raises(auth.AuthError, match="disabled"):
        auth.authenticate(migrated, session.token)


def test_logging_out_everywhere_revokes_every_session(migrated: Connection) -> None:
    user = auth.create_user(migrated, "alice@example.com", PASSWORD)
    tokens = [auth.login(migrated, "alice@example.com", PASSWORD).token for _ in range(3)]

    assert auth.logout_everywhere(migrated, user.id) == 3

    for token in tokens:
        with pytest.raises(auth.AuthError):
            auth.authenticate(migrated, token)


def test_a_session_records_when_it_was_last_used(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    session = auth.login(migrated, "alice@example.com", PASSWORD)
    migrated.execute("UPDATE sessions SET last_seen_at = now() - interval '1 hour'")

    auth.authenticate(migrated, session.token)

    with migrated.cursor() as cur:
        cur.execute("SELECT last_seen_at > now() - interval '1 minute' FROM sessions")
        assert cur.fetchone() == (True,)


def test_expired_sessions_are_purged_eventually(migrated: Connection) -> None:
    """They are already refused; this stops the table growing without bound."""
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    auth.login(migrated, "alice@example.com", PASSWORD)
    migrated.execute(
        "UPDATE sessions SET created_at = now() - interval '100 days', "
        "expires_at = now() - interval '90 days'"
    )

    assert auth.purge_expired_sessions(migrated) == 1


def test_a_live_session_is_not_purged(migrated: Connection) -> None:
    auth.create_user(migrated, "alice@example.com", PASSWORD)
    auth.login(migrated, "alice@example.com", PASSWORD)

    assert auth.purge_expired_sessions(migrated) == 0


def test_a_session_cannot_expire_before_it_starts(migrated: Connection) -> None:
    from psycopg import errors

    user = auth.create_user(migrated, "alice@example.com", PASSWORD)

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) "
            "VALUES ('x', %s, now() - interval '1 day')",
            (user.id,),
        )
    migrated.rollback()


def test_deleting_a_user_takes_their_sessions(migrated: Connection) -> None:
    user = auth.create_user(migrated, "alice@example.com", PASSWORD)
    auth.login(migrated, "alice@example.com", PASSWORD)

    migrated.execute("DELETE FROM users WHERE id = %s", (user.id,))

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM sessions")
        assert cur.fetchone() == (0,)
