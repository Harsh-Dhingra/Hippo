"""Logging in as who your company says you are.

The whole point of this module is that Hippo stops being the authority on who
somebody is. That matters here more than in most applications, because the link
from a login to a principal is an email match — so whoever gets to assert an
email decides what a session can read. An IdP asserting it after verifying the
domain is a stronger claim than a signup form, and this is where the stronger
claim is checked.

**Everything an IdP tells us arrives through the browser of the person logging
in.** The id_token, the code, the state — all of it is handed to us by the
party we are trying to authenticate. So none of it is trusted on arrival:

  * The signature is verified against keys fetched from the issuer directly,
    over a channel the browser is not in.
  * The algorithm is checked against an allowlist of asymmetric algorithms.
    Accepting `none` is the textbook bypass; accepting HMAC is the subtler one,
    where an attacker signs a token with the public key everybody already has.
  * `iss`, `aud`, `exp` and `nonce` are all checked. A token minted for another
    application, or for an earlier login attempt, is a valid token — just not
    for this.
  * The code is exchanged with PKCE, so a code lifted out of a redirect is
    useless without a verifier that never left the database.

**email_verified is a permission decision, not a profile field.** An IdP that
lets somebody type an unverified address into their own profile would otherwise
be a way to inherit a colleague's access. An unverified email logs in fine and
maps to no principal at all, which is a session that can see nothing rather
than a session that can see somebody else's things.

**Failures say little.** Whether a subject is unknown, disabled, or conflicts
with an existing account is not something a login screen should distinguish
for whoever is typing into it. The structured log carries the detail.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import httpx
import jwt
from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field

from api.auth import (
    SESSION_TTL,
    TOKEN_BYTES,
    AuthError,
    Session,
    User,
    find_principal_for,
    hash_token,
    normalise_email,
)
from core.config import Settings
from core.db import Connection

LOG = logging.getLogger("hippo.api.oidc")

LOGINS = Counter("hippo_oidc_logins_total", "OIDC login attempts.", ("outcome",))

# Asymmetric only. `none` is the textbook bypass and HMAC is the subtle one:
# with a shared-secret algorithm an attacker signs a token using the public key
# every client already has, and a naive verifier accepts it.
ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")

# A login is a person clicking through a form. Anything still in flight after
# this is abandoned, and leaving it around only widens the window for a replay.
FLOW_TTL = timedelta(minutes=10)

# Clocks drift. Small enough that an expired token stays expired.
LEEWAY_SECONDS = 60

DISCOVERY_PATH = "/.well-known/openid-configuration"

# Refetched rather than trusted forever: an IdP rotating a signing key should
# not need a restart here, and a key that has been withdrawn should stop working.
CACHE_TTL_SECONDS = 300.0


class OIDCError(AuthError):
    """A login that cannot be completed. The message is for the log, not the screen."""


class OIDCUnavailableError(OIDCError):
    """The IdP is broken or unreachable, rather than refusing this login.

    Kept apart because the two need opposite responses. "We could not verify
    who you are" sends somebody to reset a password that was never the problem;
    an outage should say it is an outage and be retried.
    """


class Provider(BaseModel):
    """What the issuer says about itself."""

    model_config = ConfigDict(frozen=True)

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None = None
    token_endpoint_auth_methods_supported: tuple[str, ...] = ()
    code_challenge_methods_supported: tuple[str, ...] = ()

    @property
    def uses_basic_auth(self) -> bool:
        """client_secret_basic is the spec default, so absence of the list means basic."""
        methods = self.token_endpoint_auth_methods_supported
        return not methods or "client_secret_basic" in methods

    @property
    def supports_pkce(self) -> bool:
        return "S256" in self.code_challenge_methods_supported


class Identity(BaseModel):
    """A verified claim set, reduced to what Hippo acts on."""

    model_config = ConfigDict(frozen=True)

    issuer: str
    subject: str
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None

    @property
    def usable_email(self) -> str | None:
        """The address permissions may be mapped from, or nothing.

        Unverified is treated as absent rather than as a weaker yes. There is no
        sensible half-measure between "this is their address" and "somebody
        typed this in".
        """
        return self.email if (self.email and self.email_verified) else None


class Start(BaseModel):
    """Where to send the browser, and the state that has to come back."""

    model_config = ConfigDict(frozen=True)

    authorization_url: str
    state: str


class _Cached(BaseModel):
    """A fetched document and when it stops being trusted."""

    model_config = ConfigDict(frozen=True)

    value: Any
    fetched_at: float


class Client:
    """One configured IdP.

    Holds the discovery document and the signing keys, both refetched on a
    timer rather than cached for the life of the process: a rotated key should
    take effect without a restart, and a withdrawn one should stop working.
    """

    def __init__(self, settings: Settings, *, http: httpx.Client | None = None) -> None:
        if not settings.oidc_issuer:
            raise OIDCError("single sign-on is not configured")
        if not (settings.oidc_client_id and settings.oidc_redirect_uri):
            raise OIDCError("single sign-on needs a client id and a redirect uri")

        self.issuer = settings.oidc_issuer.rstrip("/")
        self.client_id = settings.oidc_client_id
        self.client_secret = settings.oidc_client_secret.get_secret_value()
        self.redirect_uri = settings.oidc_redirect_uri
        self.auto_create = settings.oidc_auto_create_users
        scopes = settings.oidc_scopes.split()
        self.scopes = ["openid", *(s for s in scopes if s != "openid")]
        self._http = http or httpx.Client(timeout=10.0)
        self._provider: _Cached | None = None
        self._jwks: _Cached | None = None

    # -- Discovery ---------------------------------------------------------

    def provider(self) -> Provider:
        """The discovery document, checked for the one thing that matters in it.

        `issuer` inside the document must equal the issuer we asked. Without
        that check, a redirect on the discovery URL swaps in an entirely
        different IdP and every later signature check passes honestly against
        the wrong authority.
        """
        if self._provider is not None and not self._stale(self._provider):
            assert isinstance(self._provider.value, Provider)
            return self._provider.value

        document = self._get_json(f"{self.issuer}{DISCOVERY_PATH}")
        provider = Provider.model_validate(document)
        if provider.issuer.rstrip("/") != self.issuer:
            raise OIDCError(
                f"discovery document declares issuer {provider.issuer!r}, expected {self.issuer!r}"
            )
        self._provider = _Cached(value=provider, fetched_at=time.monotonic())
        return provider

    def _jwk_client(self) -> jwt.PyJWKClient:
        if self._jwks is not None and not self._stale(self._jwks):
            client = self._jwks.value
            assert isinstance(client, jwt.PyJWKClient)
            return client
        client = jwt.PyJWKClient(self.provider().jwks_uri, cache_keys=False)
        self._jwks = _Cached(value=client, fetched_at=time.monotonic())
        return client

    @staticmethod
    def _stale(entry: _Cached) -> bool:
        return time.monotonic() - entry.fetched_at > CACHE_TTL_SECONDS

    def _get_json(self, url: str) -> dict[str, Any]:
        response = self._http.get(url)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise OIDCError(f"{url} did not return a JSON object")
        return body

    # -- The handshake -----------------------------------------------------

    def begin(self, conn: Connection, *, redirect_to: str | None = None) -> Start:
        """Record an attempt and produce the URL to send the browser to.

        state, nonce and the PKCE verifier are generated here and stored
        server-side. They could ride in a cookie, but then single-use means
        trusting the browser to forget, and a browser doing something it was
        not asked to do is the attack all three of these defend against.
        """
        provider = self.provider()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO auth_flows (state, nonce, code_verifier, redirect_to, expires_at) "
                "VALUES (%s, %s, %s, %s, now() + %s)",
                (state, nonce, verifier, safe_redirect(redirect_to), FLOW_TTL),
            )

        parameters = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
            "nonce": nonce,
        }
        if provider.supports_pkce:
            parameters["code_challenge"] = _challenge(verifier)
            parameters["code_challenge_method"] = "S256"
        else:
            # Worth saying out loud. Without PKCE an authorization code lifted
            # from a redirect, a proxy log or a referrer header is enough on its
            # own, and every IdP worth deploying has supported it for years.
            LOG.warning("issuer does not advertise PKCE", extra={"issuer": self.issuer})

        return Start(
            authorization_url=f"{provider.authorization_endpoint}?{urlencode(parameters)}",
            state=state,
        )

    def _consume_flow(self, conn: Connection, state: str) -> tuple[str, str, str | None]:
        """Claim an in-flight login exactly once.

        The UPDATE is the claim: consumed_at is set in the same statement that
        reads the row, so two callbacks racing on one state cannot both proceed
        no matter how they interleave.
        """
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_flows SET consumed_at = now() "
                "WHERE state = %s AND consumed_at IS NULL AND expires_at > now() "
                "RETURNING nonce, code_verifier, redirect_to",
                (state,),
            )
            row = cur.fetchone()
        if row is None:
            raise OIDCError("no login is in flight for this state")
        return str(row[0]), str(row[1]), None if row[2] is None else str(row[2])

    def _exchange(self, code: str, verifier: str) -> dict[str, Any]:
        provider = self.provider()
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": verifier,
        }
        auth: httpx.Auth | None = None
        if provider.uses_basic_auth:
            auth = httpx.BasicAuth(self.client_id, self.client_secret)
        else:
            form["client_id"] = self.client_id
            form["client_secret"] = self.client_secret

        response = self._http.post(
            provider.token_endpoint,
            data=form,
            auth=auth or httpx.USE_CLIENT_DEFAULT,
        )
        if response.status_code >= 400:
            # The IdP's error body can carry the code verbatim. Logged short and
            # never returned to the browser.
            LOG.warning(
                "token exchange refused",
                extra={"status": response.status_code, "issuer": self.issuer},
            )
            # A 5xx is the IdP failing, not this login being rejected, and the
            # two want opposite answers: one is retried, the other is not.
            error = OIDCUnavailableError if response.status_code >= 500 else OIDCError
            raise error(f"token endpoint returned {response.status_code}")
        body = response.json()
        if not isinstance(body, dict):
            raise OIDCError("token endpoint did not return a JSON object")
        return body

    def verify(self, id_token: str, nonce: str) -> Identity:
        """Check a token every way that matters, then reduce it to claims.

        Order is deliberate: signature and registered claims first, through a
        library that has seen more scrutiny than anything written here, then
        nonce, which is the check that ties an otherwise perfectly valid token
        to *this* login attempt.
        """
        key = self._jwk_client().get_signing_key_from_jwt(id_token).key
        try:
            claims = jwt.decode(
                id_token,
                key=key,
                algorithms=list(ALGORITHMS),
                issuer=self.issuer,
                audience=self.client_id,
                leeway=LEEWAY_SECONDS,
                options={
                    "require": ["iss", "sub", "aud", "exp", "iat"],
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_aud": True,
                    "verify_iss": True,
                },
            )
        except jwt.InvalidTokenError as exc:
            raise OIDCError(f"id_token rejected: {exc}") from exc

        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OIDCError("id_token nonce does not match this login attempt")

        # `azp` names who the token was minted for when several clients share an
        # audience. Ours or nobody's.
        authorised_party = claims.get("azp")
        if authorised_party is not None and str(authorised_party) != self.client_id:
            raise OIDCError("id_token was issued for a different client")

        email = claims.get("email")
        return Identity(
            issuer=self.issuer,
            subject=str(claims["sub"]),
            email=normalise_email(str(email)) if email else None,
            # Absent means no. Some IdPs omit it entirely, and reading that as
            # verified would make the weakest provider set the policy.
            email_verified=claims.get("email_verified") is True,
            display_name=_first_string(claims, "name", "preferred_username", "given_name"),
        )

    # -- The whole flow ----------------------------------------------------

    def complete(
        self,
        conn: Connection,
        *,
        code: str,
        state: str,
        ttl: timedelta = SESSION_TTL,
    ) -> tuple[Session, str | None]:
        """Turn a callback into a session, or refuse.

        Returns the session and wherever the person was headed before they were
        asked to log in.
        """
        nonce, verifier, redirect_to = self._consume_flow(conn, state)
        tokens = self._exchange(code, verifier)
        id_token = tokens.get("id_token")
        if not id_token:
            raise OIDCError("token response carried no id_token")

        identity = self.verify(str(id_token), nonce)
        user = self.link(conn, identity)
        return _issue_session(conn, user, ttl=ttl), redirect_to

    # -- Mapping an identity onto a user -----------------------------------

    def link(self, conn: Connection, identity: Identity) -> User:
        """Find or create the user this verified identity denotes.

        Three cases, in order of how much they are trusted:

        1. The (issuer, subject) pair is already known. That is the identity
           key, so a changed email is a profile update and nothing more.
        2. A local user has the verified email and no IdP identity yet. This is
           the migration path off passwords, and it is why email_verified is
           enforced — without it, this branch is a way to claim somebody's
           account by typing their address into a profile.
        3. Nobody matches. Create, if the install allows it.
        """
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, email, display_name, principal_id, is_admin, disabled_at "
                "FROM users WHERE oidc_issuer = %s AND oidc_subject = %s",
                (identity.issuer, identity.subject),
            )
            row = cur.fetchone()

        if row is not None:
            return self._refresh(conn, row, identity)

        email = identity.usable_email
        if email is None:
            LOG.warning(
                "id_token carried no verified email",
                extra={"issuer": identity.issuer, "subject": identity.subject},
            )

        if email is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, email, display_name, principal_id, is_admin, disabled_at, "
                    "       oidc_subject FROM users WHERE email = %s",
                    (email,),
                )
                existing = cur.fetchone()
            if existing is not None:
                if existing[6] is not None:
                    # Same address, different IdP subject. Somebody was
                    # deprovisioned and their address reissued, or two IdPs are
                    # configured. Adopting the row would hand over its history.
                    LOG.error(
                        "refusing to rebind an account to a new subject",
                        extra={"email": email, "issuer": identity.issuer},
                    )
                    LOGINS.labels(outcome="conflict").inc()
                    raise OIDCError("this address already belongs to a different SSO identity")
                return self._adopt(conn, existing[:6], identity, email)

        if not self.auto_create:
            LOGINS.labels(outcome="unknown").inc()
            raise OIDCError("no user is provisioned for this identity")

        return self._create(conn, identity, email)

    def _refresh(self, conn: Connection, row: Any, identity: Identity) -> User:
        """A known identity logging in again."""
        user = _user(row)
        _require_enabled(user, row[5])
        email = identity.usable_email
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET "
                "    email = coalesce(%s, email), "
                "    display_name = coalesce(%s, display_name), "
                "    principal_id = coalesce(principal_id, %s), "
                "    last_login_at = now() "
                "WHERE id = %s "
                "RETURNING email, display_name, principal_id",
                (
                    email,
                    identity.display_name,
                    find_principal_for(conn, email) if email else None,
                    user.id,
                ),
            )
            updated = cur.fetchone()
        assert updated is not None
        LOGINS.labels(outcome="ok").inc()
        return user.model_copy(
            update={
                "email": str(updated[0]),
                "display_name": updated[1],
                "principal_id": None if updated[2] is None else UUID(str(updated[2])),
            }
        )

    def _adopt(self, conn: Connection, row: Any, identity: Identity, email: str) -> User:
        """A local account meeting its IdP identity for the first time.

        The password is dropped in the same statement. Leaving it would keep the
        second, slower offboarding path this fragment exists to close.
        """
        user = _user(row)
        _require_enabled(user, row[5])
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET oidc_issuer = %s, oidc_subject = %s, password_hash = NULL, "
                "    display_name = coalesce(display_name, %s), "
                "    principal_id = coalesce(principal_id, %s), last_login_at = now() "
                "WHERE id = %s RETURNING principal_id",
                (
                    identity.issuer,
                    identity.subject,
                    identity.display_name,
                    find_principal_for(conn, email),
                    user.id,
                ),
            )
            updated = cur.fetchone()
        assert updated is not None
        LOG.info("local account adopted by sso", extra={"user": str(user.id), "email": email})
        LOGINS.labels(outcome="adopted").inc()
        return user.model_copy(
            update={"principal_id": None if updated[0] is None else UUID(str(updated[0]))}
        )

    def _create(self, conn: Connection, identity: Identity, email: str | None) -> User:
        """First login from an identity nobody has seen.

        The email may be absent, so the row carries a placeholder derived from
        the subject: it satisfies the shape the table requires and matches no
        principal, which is exactly the access such a login should have.
        """
        address = email or f"{identity.subject}@{_host(identity.issuer)}.invalid"
        principal_id = find_principal_for(conn, address) if email else None
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, display_name, password_hash, principal_id, "
                "                   oidc_issuer, oidc_subject, last_login_at) "
                "VALUES (%s, %s, NULL, %s, %s, %s, now()) RETURNING id",
                (
                    address,
                    identity.display_name,
                    principal_id,
                    identity.issuer,
                    identity.subject,
                ),
            )
            row = cur.fetchone()
        assert row is not None
        LOG.info(
            "provisioned user from sso",
            extra={"email": address, "mapped": principal_id is not None},
        )
        LOGINS.labels(outcome="created").inc()
        return User(
            id=UUID(str(row[0])),
            email=address,
            display_name=identity.display_name,
            principal_id=principal_id,
            is_admin=False,
        )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _user(row: Any) -> User:
    return User(
        id=UUID(str(row[0])),
        email=str(row[1]),
        display_name=row[2],
        principal_id=None if row[3] is None else UUID(str(row[3])),
        is_admin=bool(row[4]),
    )


def _require_enabled(user: User, disabled_at: Any) -> None:
    """A disabled account is refused however good its token is.

    The IdP saying somebody is who they claim is a different question from this
    install being willing to let them in.
    """
    if disabled_at is not None:
        LOG.warning("disabled user attempted sso login", extra={"user": str(user.id)})
        LOGINS.labels(outcome="disabled").inc()
        raise OIDCError("this account is disabled")


def _issue_session(conn: Connection, user: User, *, ttl: timedelta) -> Session:
    """Mint a session token. Stored as a digest, exactly as password login does."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    expires_at = datetime.now(UTC) + ttl
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
            (hash_token(token), user.id, expires_at),
        )
    LOG.info("sso session issued", extra={"user": str(user.id)})
    return Session(token=token, user=user, expires_at=expires_at)


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _first_string(claims: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = claims.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _host(issuer: str) -> str:
    return issuer.split("://", 1)[-1].split("/", 1)[0] or "idp"


def safe_redirect(target: str | None) -> str | None:
    """Keep a post-login redirect inside this application.

    An open redirect on a login endpoint is a phishing primitive: the link is
    genuinely yours, the domain in it is genuinely yours, and it lands on
    somebody else's page. Relative paths only, and `//host` is rejected because
    browsers read it as a scheme-relative URL to another origin.
    """
    if not target:
        return None
    if not target.startswith("/") or target.startswith("//") or "\\" in target:
        LOG.warning("refusing an off-site post-login redirect", extra={"target": target[:200]})
        return None
    return target


def purge_expired_flows(conn: Connection) -> int:
    """Sweep abandoned and consumed logins.

    Most attempts finish in seconds; the rest are people who closed the tab.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM auth_flows WHERE expires_at < now()")
        return cur.rowcount


def disable_user(conn: Connection, user_id: UUID) -> int:
    """Turn an account off and drop its sessions.

    Sessions outlive the login that created them, so without the second half
    somebody removed from the IdP this morning still has a working cookie this
    afternoon. Returns how many sessions were ended.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT disable_user(%s)", (user_id,))
        row = cur.fetchone()
    ended = 0 if row is None else int(row[0])
    LOG.info("user disabled", extra={"user": str(user_id), "sessions_ended": ended})
    return ended


class SSOStatus(BaseModel):
    """What the login screen needs to know. Never carries the client secret."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = False
    label: str = Field(default="Sign in with SSO")
    passwords_enabled: bool = True


def status(settings: Settings) -> SSOStatus:
    return SSOStatus(
        enabled=bool(settings.oidc_issuer and settings.oidc_client_id),
        label=settings.oidc_button_label,
        # Kept on even with SSO configured: a self-hosted install needs a way in
        # when the IdP is the thing that is down.
        passwords_enabled=True,
    )
