"""P2-GOV-3: logging in as who the company says you are.

Two things are being tested and they are worth keeping apart.

The first is the protocol, which is only useful if it refuses things. A login
that works against a well-behaved IdP proves very little — every one of these
checks exists because a token that passes it is a real token that must still be
rejected: minted for another application, minted for an earlier attempt, minted
by a key we do not trust, or not signed at all. So the happy path is one test
and the refusals are most of the file.

The second is the fragment's actual point: an IdP login lands on the right
principal, and through it on the right person's content across every connector.
That is the last test, and it is the done-condition.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from api import oidc
from api.auth import Session
from core.config import Settings
from core.db import Connection
from tests.fake_idp import FakeIdP, RunningIdP

pytestmark = pytest.mark.requires_db

REDIRECT_URI = "https://hippo.example.com/auth/callback"


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    """Generated once. Two seconds of RSA per test would dominate the file."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def idp(signing_key: rsa.RSAPrivateKey):  # type: ignore[no-untyped-def]
    with RunningIdP(FakeIdP(private_key=signing_key)) as provider:
        yield provider


def settings_for(idp: FakeIdP, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "oidc_issuer": idp.issuer,
        "oidc_client_id": idp.client_id,
        "oidc_client_secret": idp.client_secret,
        "oidc_redirect_uri": REDIRECT_URI,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


@pytest.fixture
def client(idp: FakeIdP) -> oidc.Client:
    return oidc.Client(settings_for(idp))


def sign_in(
    conn: Connection, client: oidc.Client, idp: FakeIdP, *, redirect_to: str | None = None
) -> tuple[Session, str | None]:
    """The whole round trip, as a browser would walk it."""
    start = client.begin(conn, redirect_to=redirect_to)
    code, state = idp.authorize(start.authorization_url)
    return client.complete(conn, code=code, state=state)


# ---------------------------------------------------------------------------
# The happy path.
# ---------------------------------------------------------------------------


def test_a_verified_identity_gets_a_session(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    session, redirect_to = sign_in(migrated, client, idp)

    assert session.user.email == "person@example.com"
    assert session.user.display_name == "A Person"
    assert redirect_to is None
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT oidc_issuer, oidc_subject, password_hash, last_login_at "
            "FROM users WHERE id = %s",
            (session.user.id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert (row[0], row[1]) == (idp.issuer, "idp-subject-1")
    assert row[2] is None, "an SSO user holds no password"
    assert row[3] is not None, "and the login is dated"


def test_the_session_token_authenticates(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The same session machinery as password login, not a parallel one."""
    from api.auth import authenticate

    session, _ = sign_in(migrated, client, idp)

    assert authenticate(migrated, session.token).id == session.user.id


def test_the_token_is_stored_as_a_digest(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    session, _ = sign_in(migrated, client, idp)

    with migrated.cursor() as cur:
        cur.execute("SELECT token_hash FROM sessions WHERE user_id = %s", (session.user.id,))
        stored = (cur.fetchone() or ("",))[0]
    assert session.token not in str(stored)


def test_signing_in_again_reuses_the_identity(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    first, _ = sign_in(migrated, client, idp)
    second, _ = sign_in(migrated, client, idp)

    assert first.user.id == second.user.id
    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM users")
        assert cur.fetchone() == (1,)


def test_a_renamed_person_keeps_their_account(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Subject is the identity; email is a mapping input. Someone who changes
    their surname keeps their history instead of becoming a new user."""
    first, _ = sign_in(migrated, client, idp)
    idp.email = "renamed@example.com"

    second, _ = sign_in(migrated, client, idp)

    assert second.user.id == first.user.id
    assert second.user.email == "renamed@example.com"


# ---------------------------------------------------------------------------
# Refusing tokens that are real, but not for us.
# ---------------------------------------------------------------------------


def test_an_unsigned_token_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """alg=none. The textbook bypass, and the reason the allowlist exists."""
    idp.algorithm = "none"

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_signed_with_the_public_key_is_refused(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """Algorithm confusion, which is the subtle one: the attacker signs HS256
    using the RSA public key everyone already has, and a verifier that takes
    the algorithm from the header accepts it."""
    from cryptography.hazmat.primitives import serialization

    public_pem = idp.private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    idp.algorithm = "HS256"
    idp.signing_key = public_pem

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_from_another_key_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Correctly formed, correctly claimed, signed by somebody else."""
    idp.signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_for_another_application_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """A valid token from the same IdP, minted for a different client. Without
    the audience check, every application sharing an IdP is a way into every
    other one."""
    idp.audience_override = "some-other-client"

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_naming_another_authorised_party_is_refused(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """azp, for IdPs where several clients share an audience."""
    idp.extra_claims = {"azp": "some-other-client"}

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_from_another_issuer_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    idp.issuer_override = "https://attacker.example.com"

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_an_expired_token_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    idp.expires_in = -3600

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_missing_a_required_claim_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    idp.omit_claims = ("exp",)

    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


def test_a_token_from_an_earlier_attempt_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The nonce check. This token is genuine, current, correctly signed, and
    for this application — it just belongs to a different login."""
    idp.nonce_override = "a-nonce-from-somewhere-else"

    with pytest.raises(oidc.OIDCError, match="nonce"):
        sign_in(migrated, client, idp)


def test_clock_skew_is_tolerated_but_not_much(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Issued thirty seconds in the future is a clock. An hour is not."""
    idp.issued_at_offset = 30
    sign_in(migrated, client, idp)

    idp.issued_at_offset = 3600
    idp.expires_in = -3601
    with pytest.raises(oidc.OIDCError):
        sign_in(migrated, client, idp)


# ---------------------------------------------------------------------------
# The handshake itself.
# ---------------------------------------------------------------------------


def test_a_state_can_be_used_once(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    start = client.begin(migrated)
    code, state = idp.authorize(start.authorization_url)
    client.complete(migrated, code=code, state=state)

    with pytest.raises(oidc.OIDCError, match="in flight"):
        client.complete(migrated, code=code, state=state)


def test_an_unknown_state_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """A callback nobody started. Without this, an attacker completes a login
    of their own in somebody else's browser."""
    start = client.begin(migrated)
    code, _ = idp.authorize(start.authorization_url)

    with pytest.raises(oidc.OIDCError, match="in flight"):
        client.complete(migrated, code=code, state="a-state-we-never-issued")


def test_an_expired_flow_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    start = client.begin(migrated)
    code, state = idp.authorize(start.authorization_url)
    migrated.execute(
        "UPDATE auth_flows SET expires_at = now() - interval '1 minute' WHERE state = %s", (state,)
    )

    with pytest.raises(oidc.OIDCError, match="in flight"):
        client.complete(migrated, code=code, state=state)


def test_pkce_is_sent_and_checked(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The fake IdP recomputes the challenge from the verifier and refuses a
    mismatch, so this asserts the exchange survives a real check rather than
    that some parameter was present."""
    start = client.begin(migrated)
    assert "code_challenge=" in start.authorization_url
    assert "code_challenge_method=S256" in start.authorization_url

    code, state = idp.authorize(start.authorization_url)
    client.complete(migrated, code=code, state=state)

    assert idp.token_requests[-1]["code_verifier"]


def test_the_verifier_never_leaves_the_database(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """A code lifted from a redirect is useless without it, which is only true
    while it stays server-side."""
    start = client.begin(migrated)

    assert "code_verifier" not in start.authorization_url
    with migrated.cursor() as cur:
        cur.execute("SELECT code_verifier FROM auth_flows WHERE state = %s", (start.state,))
        stored = (cur.fetchone() or ("",))[0]
    assert stored
    assert stored not in start.authorization_url


def test_a_discovery_document_naming_another_issuer_is_refused(  # type: ignore[no-untyped-def]
    migrated: Connection, idp
) -> None:
    """Without this check a redirect on the discovery URL swaps in a different
    IdP, and every later signature check then passes honestly against the
    wrong authority."""
    idp.issuer_override = "https://elsewhere.example.com"
    client = oidc.Client(settings_for(idp))

    with pytest.raises(oidc.OIDCError, match="declares issuer"):
        client.begin(migrated)


def test_a_refused_exchange_is_reported_without_the_code(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    idp.token_status = 400

    with pytest.raises(oidc.OIDCError) as caught:
        sign_in(migrated, client, idp)
    assert "400" in str(caught.value)


def test_a_response_without_an_id_token_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """A plain OAuth token response. Access tokens say nothing about who
    somebody is, and treating one as a login is the classic confusion."""
    idp.omit_id_token = True

    with pytest.raises(oidc.OIDCError, match="id_token"):
        sign_in(migrated, client, idp)


def test_client_secret_post_is_supported(migrated: Connection, idp) -> None:  # type: ignore[no-untyped-def]
    idp.auth_methods = ("client_secret_post",)
    client = oidc.Client(settings_for(idp))

    sign_in(migrated, client, idp)

    assert idp.token_requests[-1]["client_secret"] == idp.client_secret
    assert "_auth" not in idp.token_requests[-1]


def test_client_secret_basic_keeps_the_secret_out_of_the_body(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    sign_in(migrated, client, idp)

    assert idp.token_requests[-1]["_auth"] == "basic"
    assert "client_secret" not in idp.token_requests[-1]


def test_abandoned_flows_are_swept(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    client.begin(migrated)
    migrated.execute("UPDATE auth_flows SET expires_at = now() - interval '1 hour'")

    assert oidc.purge_expired_flows(migrated) == 1


# ---------------------------------------------------------------------------
# Post-login redirects.
# ---------------------------------------------------------------------------


def test_a_relative_redirect_survives_the_round_trip(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    _, redirect_to = sign_in(migrated, client, idp, redirect_to="/actions?status=pending")

    assert redirect_to == "/actions?status=pending"


@pytest.mark.parametrize(
    "target",
    [
        "https://phishing.example.com/login",
        "//phishing.example.com/login",
        "/\\phishing.example.com",
        "http://127.0.0.1:1/",
    ],
)
def test_an_off_site_redirect_is_dropped(migrated: Connection, client, idp, target: str) -> None:  # type: ignore[no-untyped-def]
    """An open redirect on a login endpoint is a phishing primitive: the link
    is genuinely ours, the domain is genuinely ours, and it lands somewhere
    else."""
    _, redirect_to = sign_in(migrated, client, idp, redirect_to=target)

    assert redirect_to is None


# ---------------------------------------------------------------------------
# Mapping an identity onto an account.
# ---------------------------------------------------------------------------


def test_an_unverified_email_maps_to_nothing(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The one that matters most. An IdP that lets somebody type an address
    into their own profile would otherwise be a way to inherit a colleague's
    access. They log in fine; they see nothing."""
    principal = principal_with_email(migrated, "person@example.com")
    idp.email_verified = False

    session, _ = sign_in(migrated, client, idp)

    assert session.user.principal_id is None, f"must not map to {principal}"


def test_a_missing_email_verified_claim_is_read_as_no(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Some IdPs omit it. Reading absence as verified would let the weakest
    provider set the policy."""
    principal_with_email(migrated, "person@example.com")
    idp.email_verified = None

    session, _ = sign_in(migrated, client, idp)

    assert session.user.principal_id is None


def test_a_verified_email_maps_to_its_principal(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    principal = principal_with_email(migrated, "person@example.com")

    session, _ = sign_in(migrated, client, idp)

    assert session.user.principal_id == principal


def test_an_identity_with_no_email_gets_no_principal(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The row still has to satisfy the users table, so it carries a
    placeholder derived from the subject — which matches no principal, and
    that is exactly the access such a login should have."""
    idp.email = None
    idp.email_verified = None

    session, _ = sign_in(migrated, client, idp)

    assert session.user.principal_id is None
    assert session.user.email.endswith(".invalid")


def test_a_local_account_is_adopted_and_loses_its_password(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """The migration path off passwords. Dropping the hash matters: leaving it
    would keep the second, slower offboarding path this fragment closes."""
    from api.auth import create_user

    existing = create_user(migrated, "person@example.com", "a-password", display_name="Old Name")

    session, _ = sign_in(migrated, client, idp)

    assert session.user.id == existing.id
    with migrated.cursor() as cur:
        cur.execute("SELECT password_hash, oidc_subject FROM users WHERE id = %s", (existing.id,))
        row = cur.fetchone()
    assert row is not None
    assert row[0] is None
    assert row[1] == "idp-subject-1"


def test_the_adopted_account_can_no_longer_use_its_password(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    from api.auth import AuthError, create_user, login

    create_user(migrated, "person@example.com", "a-password")
    sign_in(migrated, client, idp)

    with pytest.raises(AuthError):
        login(migrated, "person@example.com", "a-password")


def test_a_reissued_address_does_not_inherit_an_account(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """Somebody left, their address was reassigned, and the new holder signs
    in. Adopting the row would hand over its history."""
    sign_in(migrated, client, idp)
    idp.subject = "a-different-subject"

    with pytest.raises(oidc.OIDCError, match="different SSO identity"):
        sign_in(migrated, client, idp)


def test_a_disabled_account_is_refused(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The IdP saying who somebody is and this install being willing to let
    them in are different questions."""
    session, _ = sign_in(migrated, client, idp)
    migrated.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (session.user.id,))

    with pytest.raises(oidc.OIDCError, match="disabled"):
        sign_in(migrated, client, idp)


def test_disabling_ends_live_sessions(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Sessions outlive the login that created them. Without this, somebody
    removed from the IdP this morning still has a working cookie this
    afternoon, which is the failure this fragment exists to close."""
    from api.auth import AuthError, authenticate

    session, _ = sign_in(migrated, client, idp)

    assert oidc.disable_user(migrated, session.user.id) == 1
    with pytest.raises(AuthError):
        authenticate(migrated, session.token)


def test_provisioning_can_be_required(migrated: Connection, idp) -> None:  # type: ignore[no-untyped-def]
    """For an install that treats signup as an approval."""
    client = oidc.Client(settings_for(idp, oidc_auto_create_users=False))

    with pytest.raises(oidc.OIDCError, match="no user is provisioned"):
        sign_in(migrated, client, idp)


def test_a_principal_synced_later_is_picked_up(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """Signing up before sync has met you must not mean blind forever."""
    first, _ = sign_in(migrated, client, idp)
    assert first.user.principal_id is None

    principal = principal_with_email(migrated, "person@example.com")
    second, _ = sign_in(migrated, client, idp)

    assert second.user.principal_id == principal


# ---------------------------------------------------------------------------
# Configuration.
# ---------------------------------------------------------------------------


def test_sso_is_off_until_it_is_configured() -> None:
    assert oidc.status(Settings()).enabled is False
    with pytest.raises(oidc.OIDCError, match="not configured"):
        oidc.Client(Settings())


def test_an_issuer_without_a_client_is_refused() -> None:
    with pytest.raises(oidc.OIDCError, match="client id"):
        oidc.Client(Settings(oidc_issuer="https://acme.example.com"))


def test_the_status_endpoint_carries_no_secret(idp) -> None:  # type: ignore[no-untyped-def]
    status = oidc.status(settings_for(idp))

    assert status.enabled is True
    assert status.passwords_enabled is True, "a self-hoster needs a way in when the IdP is down"
    assert idp.client_secret not in status.model_dump_json()


def test_openid_scope_is_always_requested(idp) -> None:  # type: ignore[no-untyped-def]
    """Without it an IdP returns an OAuth response and no id_token at all."""
    client = oidc.Client(settings_for(idp, oidc_scopes="email profile"))

    assert client.scopes[0] == "openid"


def test_a_provider_without_pkce_still_works(migrated: Connection, idp) -> None:  # type: ignore[no-untyped-def]
    """Degraded, logged, and not a hard failure — some old IdPs do not
    advertise it."""
    idp.advertise_pkce = False
    client = oidc.Client(settings_for(idp))

    start = client.begin(migrated)

    assert "code_challenge" not in start.authorization_url
    code, state = idp.authorize(start.authorization_url)
    assert client.complete(migrated, code=code, state=state)[0].token


# ---------------------------------------------------------------------------
# The done-condition.
# ---------------------------------------------------------------------------


def principal_with_email(
    conn: Connection, email: str, source_id: str = "U-1", connector: str | None = None
) -> UUID:
    """One account. `connector` names which system it came from.

    Without one the principal is platform-native, and a unique index on
    lower(email) allows exactly one of those per address — which is correct, and
    which is why a person's two connector accounts each need their connector.
    """
    connector_id = None
    if connector is not None:
        connector_id = uuid4()
        conn.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, %s, %s)",
            (connector_id, connector, connector),
        )
    principal = uuid4()
    conn.execute(
        "INSERT INTO principals (id, kind, email, source_id, connector_id) "
        "VALUES (%s, 'user', %s, %s, %s)",
        (principal, email, source_id, connector_id),
    )
    return principal


def test_an_sso_login_sees_every_account_the_person_holds(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """The fragment, stated as a query.

    One human, two connector accounts, one thing granted to each. An IdP login
    has to reach both, or "principals mapped to IdP identities" means nothing
    beyond a row in a table.
    """
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings
    from resolver.resolution import link_principal_identities

    slack = principal_with_email(migrated, "person@example.com", "U-SLACK", "slack")
    jira = principal_with_email(migrated, "person@example.com", "U-JIRA", "jira")
    for principal, text in (
        (slack, "the renewal is blocked on the liability cap in legal review"),
        (jira, "ACME-1 tracks the liability cap and is assigned to legal"),
    ):
        entity = uuid4()
        migrated.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'm')", (entity,)
        )
        migrated.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
            (entity, principal),
        )
        migrated.execute(
            "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
            "VALUES (%s, '00000000-0000-0000-0000-000000000001', %s, 0)",
            (entity, text),
        )
    link_principal_identities(migrated)

    session, _ = sign_in(migrated, client, idp)

    assert session.user.principal_id in (slack, jira)
    assert session.user.principal_id is not None
    hits = retrieve(
        migrated,
        session.user.principal_id,
        RetrievalPlan(query_text="liability cap", k=10, hops=0),
        HashingEmbeddings(),
    )
    assert len(hits) == 2, "one login, both connectors"


def test_the_link_survives_a_rename_at_the_idp(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """A changed email must not silently detach somebody from their content.

    It also must not attach them to somebody else's: the new address maps to
    whatever principal holds it, which is what an email-keyed permission model
    means, and the reason the address has to come from a verified claim.
    """
    original = principal_with_email(migrated, "person@example.com", "U-SLACK", "slack")
    session, _ = sign_in(migrated, client, idp)
    assert session.user.principal_id == original

    idp.email = "renamed@example.com"
    again, _ = sign_in(migrated, client, idp)

    assert again.user.principal_id == original, "an existing link is never dropped"


def test_a_session_with_no_principal_is_not_a_session_that_sees_everything(  # type: ignore[no-untyped-def]
    migrated: Connection, client, idp
) -> None:
    """The failure mode worth naming: an unmapped login must see nothing, not
    default to unfiltered."""
    from agent.retrieval import RetrievalPlan, retrieve
    from resolver.embeddings import HashingEmbeddings

    entity = uuid4()
    migrated.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'm')", (entity,)
    )
    migrated.execute(
        "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
        "VALUES (%s, '00000000-0000-0000-0000-000000000001', 'a secret about the cap', 0)",
        (entity,),
    )
    session, _ = sign_in(migrated, client, idp)
    assert session.user.principal_id is None

    hits = retrieve(
        migrated,
        uuid4(),
        RetrievalPlan(query_text="secret", k=10, hops=0),
        HashingEmbeddings(),
    )

    assert hits == []


def test_the_id_token_is_never_stored(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """It is a bearer credential for the IdP session. Nothing here needs it
    after verification, so nothing here keeps it."""
    start = client.begin(migrated)
    code, state = idp.authorize(start.authorization_url)
    id_token = idp.id_token(code)
    client.complete(migrated, code=code, state=state)

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM users u, sessions s "
            "WHERE u.email || coalesce(u.oidc_subject, '') || s.token_hash LIKE %s",
            (f"%{id_token[:40]}%",),
        )
        assert cur.fetchone() == (0,)


def test_the_verifier_checks_a_real_signature(idp) -> None:  # type: ignore[no-untyped-def]
    """A guard on the guard: if the fixture ever stopped signing properly, most
    of the refusal tests above would pass for the wrong reason."""
    client = oidc.Client(settings_for(idp))
    code, _ = idp.authorize(client_url(client))
    token = idp.id_token(code)

    header = jwt.get_unverified_header(token)

    assert header["alg"] == "RS256"
    assert header["kid"] == idp.kid


def client_url(client: oidc.Client) -> str:
    """An authorization URL without touching the database."""
    provider = client.provider()
    return f"{provider.authorization_endpoint}?state=s&nonce=n"


def test_a_session_expires(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """An SSO session is a session: it ages out like any other."""
    from api.auth import AuthError, authenticate

    session, _ = sign_in(migrated, client, idp)
    migrated.execute(
        "UPDATE sessions SET created_at = now() - interval '8 days', "
        "expires_at = now() - interval '1 day' WHERE user_id = %s",
        (session.user.id,),
    )

    with pytest.raises(AuthError):
        authenticate(migrated, session.token)


def test_a_session_cannot_be_issued_already_expired(migrated: Connection, client, idp) -> None:  # type: ignore[no-untyped-def]
    """The database refuses it, which is where that rule belongs: a caller
    passing a bad TTL should not be able to mint a token nothing will accept.
    """
    from psycopg import errors

    start = client.begin(migrated)
    code, state = idp.authorize(start.authorization_url)

    with pytest.raises(errors.CheckViolation):
        client.complete(migrated, code=code, state=state, ttl=timedelta(seconds=-1))
    migrated.rollback()


# ---------------------------------------------------------------------------
# Over HTTP, which is how it will actually be used.
# ---------------------------------------------------------------------------


@pytest.fixture
def sso_client(db_dsn: str, idp, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """The real app with SSO configured, so the routes are exercised as shipped."""
    from fastapi.testclient import TestClient

    from api.main import create_app
    from resolver.embeddings import HashingEmbeddings

    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())
    configured = settings_for(
        idp, database_url=db_dsn, log_level="WARNING", service_name="hippo-test"
    )
    with TestClient(create_app(configured)) as client:
        yield client


def test_the_login_screen_can_ask_whether_sso_exists(sso_client, idp) -> None:  # type: ignore[no-untyped-def]
    """Unauthenticated on purpose: nobody has a session yet when the login
    screen is drawn."""
    response = sso_client.get("/api/v1/auth/sso")

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "label": "Sign in with SSO",
        "passwords_enabled": True,
    }
    assert idp.client_secret not in response.text


def test_the_status_is_reported_when_sso_is_off(db_dsn: str, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from fastapi.testclient import TestClient

    from api.main import create_app
    from resolver.embeddings import HashingEmbeddings

    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())
    with TestClient(create_app(Settings(database_url=db_dsn, log_level="WARNING"))) as client:
        assert client.get("/api/v1/auth/sso").json()["enabled"] is False
        assert client.post("/api/v1/auth/sso/start", json={}).status_code == 404


def test_the_whole_flow_over_http(sso_client, idp) -> None:  # type: ignore[no-untyped-def]
    started = sso_client.post("/api/v1/auth/sso/start", json={"redirect_to": "/actions"})
    assert started.status_code == 201

    code, state = idp.authorize(started.json()["authorization_url"])
    finished = sso_client.post("/api/v1/auth/sso/callback", json={"code": code, "state": state})

    assert finished.status_code == 201
    body = finished.json()
    assert body["user"]["email"] == "person@example.com"
    assert body["redirect_to"] == "/actions"

    me = sso_client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "person@example.com"


def test_every_refusal_looks_the_same_from_outside(sso_client, idp) -> None:  # type: ignore[no-untyped-def]
    """Which check failed is exactly the sort of detail that turns a login
    endpoint into an account enumeration oracle. One status, one message."""
    started = sso_client.post("/api/v1/auth/sso/start", json={})
    code, state = idp.authorize(started.json()["authorization_url"])

    replayed = sso_client.post("/api/v1/auth/sso/callback", json={"code": code, "state": state})
    assert replayed.status_code == 201

    responses = [
        sso_client.post("/api/v1/auth/sso/callback", json={"code": code, "state": state}),
        sso_client.post("/api/v1/auth/sso/callback", json={"code": "made-up", "state": "made-up"}),
    ]

    assert {r.status_code for r in responses} == {401}
    assert {r.json()["detail"] for r in responses} == {"could not sign you in"}


def test_a_broken_idp_is_not_reported_as_a_bad_password(sso_client, idp) -> None:  # type: ignore[no-untyped-def]
    """502, not 401. An outage that reads as "wrong credentials" sends everyone
    to reset a password that was never the problem.

    The distinction is by status class, because it is the only signal that
    separates the two: a 4xx is the IdP refusing this login, a 5xx is the IdP
    failing at its job.
    """
    started = sso_client.post("/api/v1/auth/sso/start", json={})
    code, state = idp.authorize(started.json()["authorization_url"])
    idp.token_status = 503

    response = sso_client.post("/api/v1/auth/sso/callback", json={"code": code, "state": state})

    assert response.status_code == 502
    assert response.json()["detail"] == "the identity provider is unreachable"


def test_a_refused_grant_is_still_a_401(sso_client, idp) -> None:  # type: ignore[no-untyped-def]
    """The other side of the same line."""
    started = sso_client.post("/api/v1/auth/sso/start", json={})
    code, state = idp.authorize(started.json()["authorization_url"])
    idp.token_status = 400

    response = sso_client.post("/api/v1/auth/sso/callback", json={"code": code, "state": state})

    assert response.status_code == 401
