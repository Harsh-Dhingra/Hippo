-- ============================================================
-- Hippo — Logging in as who your company says you are (P2-GOV-3)
--
-- Until now a user was an email and a password Hippo stored. That is the wrong
-- authority for an enterprise deployment in two specific ways.
--
-- WHO SAYS YOU LEFT
--
-- When somebody leaves, their access should end when IT disables the account,
-- not when an administrator remembers this system exists. A local password is
-- a second, slower offboarding path that nobody audits.
--
-- WHO SAYS WHICH EMAIL IS YOURS
--
-- The link from a login to a principal is an email match, because an email is
-- the only thing connectors agree on. That makes email an authorisation input,
-- so whoever gets to assert one decides what you can read. Hippo asserting it
-- from a signup form is weaker than an IdP asserting it after verifying the
-- domain, and this migration is what lets the stronger claim be recorded.
--
-- SUBJECT, NOT EMAIL, IS THE IDENTITY
--
-- An IdP subject is stable across a rename; an email is not. Store both, key
-- on (issuer, subject), and treat email as the mapping input it is. A person
-- who changes their surname keeps their history instead of becoming a new
-- user with an empty one.
--
-- PASSWORDS STAY, DELIBERATELY
--
-- Self-hosted means somebody has to be able to get in on a laptop with no IdP
-- in front of it. password_hash becomes nullable rather than disappearing, and
-- a CHECK requires at least one credential, so a user with neither is a shape
-- the database refuses rather than an account nobody can ever log in to.
-- ============================================================

ALTER TABLE users
    ALTER COLUMN password_hash DROP NOT NULL,
    ADD COLUMN oidc_issuer   text,
    ADD COLUMN oidc_subject  text,
    ADD COLUMN last_login_at timestamptz;

-- Half an OIDC identity is not an identity. A subject means nothing without
-- the issuer that minted it: two IdPs will happily both call somebody "1001".
ALTER TABLE users ADD CONSTRAINT users_oidc_identity_is_whole
    CHECK (num_nonnulls(oidc_issuer, oidc_subject) <> 1);

ALTER TABLE users ADD CONSTRAINT users_has_a_credential
    CHECK (password_hash IS NOT NULL OR oidc_subject IS NOT NULL);

-- The identity key. Not email: an IdP subject survives a rename, and keying on
-- email would hand a leaver's history to whoever inherits their address.
CREATE UNIQUE INDEX users_oidc_key ON users (oidc_issuer, oidc_subject)
    WHERE oidc_subject IS NOT NULL;

COMMENT ON COLUMN users.oidc_subject IS
    'The IdP''s stable identifier for this person. Authoritative over email, '
    'which is a mapping input rather than an identity.';

-- ------------------------------------------------------------
-- The handshake, held server-side
-- ------------------------------------------------------------
-- state, nonce and the PKCE verifier have to survive a redirect to the IdP and
-- come back verifiable, and all three are single-use. A cookie could carry
-- them, but then "single-use" means trusting the browser to forget — and the
-- attack these defend against is a browser that has been made to do something
-- it was not asked to do.
--
-- Rows here are consumed on callback and swept afterwards, so this stays small
-- and there is no queue, no cache and no second datastore involved.
CREATE TABLE auth_flows (
    -- Opaque, high-entropy, generated per attempt. Primary key because a state
    -- appearing twice is either a replay or a bug, and both should collide.
    state         text PRIMARY KEY,
    -- Bound into the id_token by the IdP, so a token minted for a different
    -- login attempt cannot be replayed into this one.
    nonce         text NOT NULL,
    -- PKCE. Defends the code itself: an authorization code stolen from a
    -- redirect is useless without the verifier, which never leaves this table.
    code_verifier text NOT NULL,
    -- Where the person was going before they were asked to log in. Validated
    -- as a relative path on the way out, never a URL: an open redirect on a
    -- login endpoint is a phishing primitive.
    redirect_to   text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    consumed_at   timestamptz,

    CONSTRAINT auth_flows_redirect_is_relative
        CHECK (redirect_to IS NULL OR redirect_to LIKE '/%' AND redirect_to NOT LIKE '//%')
);
CREATE INDEX auth_flows_expiry_idx ON auth_flows (expires_at);

COMMENT ON TABLE auth_flows IS
    'One in-flight OIDC login each. Single-use: consumed_at is set on callback '
    'and a second presentation of the same state is refused.';

GRANT SELECT, INSERT, UPDATE, DELETE ON auth_flows TO hippo_api;

-- ------------------------------------------------------------
-- Disabling, so offboarding has somewhere to land
-- ------------------------------------------------------------
-- Sessions outlive the login that created them, so disabling a user has to
-- reach the sessions too. Otherwise the seven-day cookie of somebody removed
-- from the IdP this morning still works this afternoon, which is the exact
-- failure this fragment exists to close.
CREATE FUNCTION disable_user(p_user uuid) RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
DECLARE
    dropped integer;
BEGIN
    UPDATE users SET disabled_at = coalesce(disabled_at, now()) WHERE id = p_user;
    DELETE FROM sessions WHERE user_id = p_user;
    GET DIAGNOSTICS dropped = ROW_COUNT;
    RETURN dropped;
END;
$$;

REVOKE ALL ON FUNCTION disable_user(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION disable_user(uuid) TO hippo_api;
