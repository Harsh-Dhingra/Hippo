-- ============================================================
-- Hippo — Platform users, sessions, and the role that serves them (P1-SRF-1)
--
-- STACK.md: "Session auth v0 → OIDC at P2-GOV-3 → SCIM at P4-ENT-1", with
-- "building auth cleverness early" as the stated failure mode. So this is the
-- boring version, and the only interesting decisions are the ones that would
-- be expensive to change later.
--
-- A USER IS NOT A PRINCIPAL
--
-- principals are accounts in source systems, created by sync, and a grant is
-- something Slack or Jira asserts about one of them. A user is a person who
-- logs in here. Keeping them separate means signing up for Hippo grants
-- nothing: a user with no matching principal can log in and see exactly
-- nothing, which is the correct deny-by-default reading of "we have never
-- heard of you".
--
-- The join is by email, the same rule identity resolution uses everywhere
-- else, and it only needs to find *one* principal: migration 008's identity
-- linking expands from there to the person's other accounts. users.principal_id
-- is that starting point, and it is nullable on purpose — an unmatched user is
-- a normal state, not an error, and often just means sync has not run yet.
--
-- SESSIONS STORE A HASH, NOT A TOKEN
--
-- The token exists in the client's hands and nowhere else. A dump of this table
-- yields nothing that can be replayed, which is the same reason the password
-- column holds a KDF output rather than a password. Both use scrypt from the
-- standard library: a real KDF, no dependency to audit, and the parameters are
-- stored alongside each hash so they can be raised without invalidating
-- everyone's password.
--
-- THE API ROLE
--
-- hippo_api serves users, sessions and approvals. It deliberately does NOT get
-- SELECT on chunks, entities or raw_records: the API process runs agent queries
-- under SET LOCAL ROLE hippo_agent, so content still reaches a prompt only
-- through visible_chunks(). What hippo_api can do that hippo_agent cannot is
-- approve an action — which is the point, because approval is the human's
-- decision and the agent must never be able to make it.
-- ============================================================

CREATE TABLE users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email          text NOT NULL,
    display_name   text,
    password_hash  text NOT NULL,
    -- Where this person's permissions come from. NULL means nothing is visible
    -- to them yet, which is a state to report, not an error to raise.
    principal_id   uuid REFERENCES principals(id),
    is_admin       boolean NOT NULL DEFAULT false,
    disabled_at    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT users_email_is_normalised CHECK (email = lower(btrim(email))),
    CONSTRAINT users_email_looks_like_one CHECK (email LIKE '%_@_%')
);
CREATE UNIQUE INDEX users_email_key ON users (email);
CREATE TRIGGER users_touch BEFORE UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

COMMENT ON COLUMN users.password_hash IS
    'scrypt output with its parameters and salt, never a password and never a '
    'bare digest.';

CREATE TABLE sessions (
    token_hash   text PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    revoked_at   timestamptz,

    CONSTRAINT sessions_expire_after_they_start CHECK (expires_at > created_at)
);
CREATE INDEX ON sessions (user_id);
CREATE INDEX ON sessions (expires_at);

COMMENT ON TABLE sessions IS
    'The token itself is never stored. This holds sha256 of it, so a dump of '
    'this table yields nothing replayable.';

-- ------------------------------------------------------------
-- Declining is not failing
-- ------------------------------------------------------------
-- 001 gave actions five statuses, and 'failed' was the only home for an action
-- a person looked at and said no to. That loses the distinction the audit log
-- most needs to keep: an action that nobody wanted and an action that broke are
-- different events, and only one of them is worth investigating.
ALTER TABLE actions ADD COLUMN declined_by uuid REFERENCES principals(id);

ALTER TABLE actions DROP CONSTRAINT actions_status_known;
ALTER TABLE actions ADD CONSTRAINT actions_status_known
    CHECK (status IN ('pending', 'approved', 'declined', 'executed', 'rolled_back', 'failed'));

-- A decline is a decision, so it names the person who made it, exactly as an
-- approval does.
ALTER TABLE actions ADD CONSTRAINT actions_declined_names_the_decliner
    CHECK (status <> 'declined' OR declined_by IS NOT NULL);

ALTER TABLE actions DROP CONSTRAINT actions_execution_requires_approval;
ALTER TABLE actions ADD CONSTRAINT actions_execution_requires_approval
    CHECK (status IN ('pending', 'declined', 'failed') OR approved_by IS NOT NULL);

-- ------------------------------------------------------------
-- hippo_api
-- ------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hippo_api') THEN
        CREATE ROLE hippo_api NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA public TO hippo_api;
REVOKE CREATE ON SCHEMA public FROM hippo_api;

GRANT SELECT, INSERT, UPDATE, DELETE ON users, sessions TO hippo_api;

-- Approval is the human's decision, so the role that serves humans is the one
-- that can record it. It cannot INSERT an action: proposals come from the
-- agent, and an API that could mint its own would make the propose/approve
-- split decorative.
GRANT SELECT, UPDATE ON actions TO hippo_api;

-- Enough to render an approval: what the action points at, and who asked.
GRANT SELECT ON entities, connectors, principals TO hippo_api;

-- The API serves the trace view, and reads it the same way the agent writes
-- it — through the owner-filtered functions, not a SELECT.
GRANT EXECUTE ON FUNCTION my_traces(uuid, int) TO hippo_api;
GRANT EXECUTE ON FUNCTION my_trace(uuid, uuid) TO hippo_api;

-- Deliberately NOT `GRANT hippo_agent TO hippo_api`. Membership between the
-- two group roles would let hippo_api call visible_chunks() under its own
-- privileges, and then the SET LOCAL ROLE in api/routes.py would be a gesture
-- rather than a reduction. Membership belongs on the operator's login user,
-- the same way 002 puts it:
--
--   CREATE USER hippo_api_svc LOGIN PASSWORD '...';
--   GRANT hippo_api, hippo_agent TO hippo_api_svc;
--
-- The process then starts as hippo_api and becomes hippo_agent for exactly the
-- span of an agent query.
