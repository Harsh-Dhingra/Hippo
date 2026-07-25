"""Users, passwords and sessions.

STACK.md puts session auth in v0 and names the failure mode: building auth
cleverness early. So this is the boring version. The parts worth being careful
about are the ones that are expensive to change once people have accounts.

**Passwords go through a KDF.** scrypt from the standard library — a real
memory-hard KDF, no dependency to audit, and the cost parameters are stored
next to each hash so raising them later does not invalidate anyone's password.
Comparison is constant-time.

**Sessions store a hash of the token, not the token.** The token exists in the
client's hands and nowhere else, so a dump of the sessions table yields nothing
replayable. sha256 rather than scrypt here is deliberate and not an
inconsistency: a session token is 256 bits of `secrets` output, so there is no
low-entropy guess for a slow hash to defend against, and login checks a
password once while every request checks a session.

**A user is not a principal.** Signing up grants nothing. The link is by email,
the same rule identity resolution uses, and finding one principal is enough —
migration 008 expands from there to the person's other accounts. A user with no
match logs in fine and sees nothing, which is the right answer to "we have never
heard of you".
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from core.db import Connection

LOG = logging.getLogger("hippo.api.auth")

# scrypt parameters. n=2**15 is roughly 100ms and ~32MB per hash on the machines
# this runs on, which is the usual balance for an interactive login.
SCRYPT_N = 2**15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_KEYLEN = 32
SALT_BYTES = 16

TOKEN_BYTES = 32
SESSION_TTL = timedelta(days=7)


class AuthError(Exception):
    """Authentication failed.

    One exception for every reason, and the message is deliberately vague at
    the boundary: "no such user" and "wrong password" are the same answer to
    anyone asking, or the login form becomes a way to enumerate accounts.
    """


class User(BaseModel):
    """A person who logs in."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    email: str
    display_name: str | None
    principal_id: UUID | None
    is_admin: bool = False

    @property
    def can_see_anything(self) -> bool:
        """False until sync has met this person. Not an error, a state."""
        return self.principal_id is not None


def normalise_email(email: str) -> str:
    return email.strip().lower()


def _scrypt(password: str, salt: bytes, n: int, r: int, p: int, dklen: int) -> bytes:
    """scrypt with a memory budget derived from the parameters.

    OpenSSL defaults maxmem to 32 MiB and scrypt needs 128*n*r, which at
    n=2**15, r=8 is exactly 32 MiB — so the default fails on the very
    parameters that make the KDF worth using. Deriving the budget from the
    stored parameters rather than pinning a constant means raising the cost
    later still verifies every hash written before.
    """
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=dklen,
        maxmem=128 * n * r * 2,
    )


def hash_password(password: str) -> str:
    """scrypt, with the parameters recorded so they can be raised later."""
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P, SCRYPT_KEYLEN)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check against a stored hash.

    An unparseable hash is a failed check rather than an exception: a corrupt
    row should lock one account out, not return a 500 that says which account
    is corrupt.
    """
    try:
        scheme, n, r, p, salt_hex, digest_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = _scrypt(password, bytes.fromhex(salt_hex), int(n), int(r), int(p), len(expected))
    except (ValueError, TypeError):
        LOG.warning("a stored password hash could not be read")
        return False
    return hmac.compare_digest(actual, expected)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Users.
# ---------------------------------------------------------------------------


def find_principal_for(conn: Connection, email: str) -> UUID | None:
    """One principal with this email, if sync has seen one.

    Any match will do. The permission filter expands from a principal to every
    other account the same human holds, so picking a different one would return
    the same visible set. Ordered by id only so the choice is stable.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM principals "
            "WHERE kind = 'user' AND lower(btrim(email)) = %s ORDER BY id LIMIT 1",
            (normalise_email(email),),
        )
        row = cur.fetchone()
    return None if row is None else UUID(str(row[0]))


def create_user(
    conn: Connection,
    email: str,
    password: str,
    *,
    display_name: str | None = None,
    is_admin: bool = False,
) -> User:
    """Register a person. Grants nothing on its own."""
    address = normalise_email(email)
    principal_id = find_principal_for(conn, address)
    if principal_id is None:
        LOG.info("new user has no matching principal yet", extra={"email": address})

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO users (email, display_name, password_hash, principal_id, is_admin) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (address, display_name, hash_password(password), principal_id, is_admin),
        )
        row = cur.fetchone()
    assert row is not None
    return User(
        id=UUID(str(row[0])),
        email=address,
        display_name=display_name,
        principal_id=principal_id,
        is_admin=is_admin,
    )


def relink_principals(conn: Connection) -> int:
    """Attach users to principals that sync has since created.

    Someone who signed up before their Slack account was synced would otherwise
    stay blind forever. Run after a sync; it never detaches an existing link,
    because a principal disappearing from a source is a revocation question and
    revocation is the ACL fast-lane's job, not this function's.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE users u SET principal_id = p.id "
            "FROM principals p "
            "WHERE u.principal_id IS NULL AND p.kind = 'user' "
            "  AND lower(btrim(p.email)) = u.email"
        )
        linked = cur.rowcount
    if linked:
        LOG.info("linked users to principals", extra={"users": linked})
    return linked


# ---------------------------------------------------------------------------
# Sessions.
# ---------------------------------------------------------------------------


class Session(BaseModel):
    """A logged-in session. The token is returned once and never stored."""

    model_config = ConfigDict(frozen=True)

    token: str
    user: User
    expires_at: datetime


def login(conn: Connection, email: str, password: str, *, ttl: timedelta = SESSION_TTL) -> Session:
    """Check a password and issue a session token."""
    address = normalise_email(email)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, email, display_name, password_hash, principal_id, is_admin, disabled_at "
            "FROM users WHERE email = %s",
            (address,),
        )
        row = cur.fetchone()

    if row is None:
        # Spend the time anyway. Answering faster for an unknown address turns
        # the login form into a way to enumerate who has an account.
        verify_password(password, hash_password("timing"))
        raise AuthError("invalid email or password")

    if not verify_password(password, str(row[3])):
        raise AuthError("invalid email or password")
    if row[6] is not None:
        raise AuthError("this account is disabled")

    user = User(
        id=UUID(str(row[0])),
        email=str(row[1]),
        display_name=None if row[2] is None else str(row[2]),
        principal_id=None if row[4] is None else UUID(str(row[4])),
        is_admin=bool(row[5]),
    )
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = datetime.now(UTC) + ttl
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (hash_token(token), user.id, expires_at),
        )

    LOG.info("session opened", extra={"user_id": str(user.id)})
    return Session(token=token, user=user, expires_at=expires_at)


def authenticate(conn: Connection, token: str) -> User:
    """Resolve a token to a user, or raise.

    Reads the user through the session row rather than trusting anything the
    client sent, and re-reads `disabled_at` and `principal_id` every request:
    disabling an account or revoking a principal must take effect now, not at
    the next login.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT u.id, u.email, u.display_name, u.principal_id, u.is_admin, u.disabled_at, "
            "       s.expires_at, s.revoked_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token_hash = %s",
            (hash_token(token),),
        )
        row = cur.fetchone()

    if row is None:
        raise AuthError("not authenticated")
    if row[7] is not None:
        raise AuthError("this session has been signed out")
    if row[6] <= datetime.now(UTC):
        raise AuthError("this session has expired")
    if row[5] is not None:
        raise AuthError("this account is disabled")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET last_seen_at = now() WHERE token_hash = %s", (hash_token(token),)
        )

    return User(
        id=UUID(str(row[0])),
        email=str(row[1]),
        display_name=None if row[2] is None else str(row[2]),
        principal_id=None if row[3] is None else UUID(str(row[3])),
        is_admin=bool(row[4]),
    )


def logout(conn: Connection, token: str) -> bool:
    """Revoke one session. Idempotent."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now() WHERE token_hash = %s AND revoked_at IS NULL",
            (hash_token(token),),
        )
        return cur.rowcount > 0


def logout_everywhere(conn: Connection, user_id: UUID) -> int:
    """Revoke every session a user holds. What a compromised laptop needs."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE sessions SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return cur.rowcount


def purge_expired_sessions(conn: Connection) -> int:
    """Housekeeping. Expired rows are already refused; this stops the table
    growing without bound."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM sessions WHERE expires_at < now() - interval '30 days'")
        return cur.rowcount
