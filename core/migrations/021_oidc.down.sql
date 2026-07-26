-- Reverses 021_oidc.sql.
--
-- Not lossless, and it cannot be. password_hash goes back to NOT NULL, so a
-- user who only ever had an IdP identity has no credential to fall back to.
-- Rather than invent one or fail on a constraint violation halfway through,
-- those rows are removed and the removal is announced.

DROP FUNCTION IF EXISTS disable_user(uuid);
DROP TABLE IF EXISTS auth_flows;

DO $$
DECLARE
    orphaned integer;
BEGIN
    SELECT count(*) INTO orphaned FROM users WHERE password_hash IS NULL;
    IF orphaned > 0 THEN
        RAISE WARNING 'removing % SSO-only user(s): password_hash is about to be NOT NULL again',
            orphaned;
        DELETE FROM users WHERE password_hash IS NULL;
    END IF;
END;
$$;

ALTER TABLE users
    DROP CONSTRAINT IF EXISTS users_has_a_credential,
    DROP CONSTRAINT IF EXISTS users_oidc_identity_is_whole;

DROP INDEX IF EXISTS users_oidc_key;

ALTER TABLE users
    DROP COLUMN IF EXISTS oidc_issuer,
    DROP COLUMN IF EXISTS oidc_subject,
    DROP COLUMN IF EXISTS last_login_at;

ALTER TABLE users ALTER COLUMN password_hash SET NOT NULL;
