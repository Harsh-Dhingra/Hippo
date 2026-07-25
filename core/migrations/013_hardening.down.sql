-- Reverts 013_hardening.sql.
--
-- Both directions widen something, which is why this is worth reading before
-- running: the API regains a read path to entity titles, and the database stops
-- refusing to store a credential. Neither is a state to sit in.

ALTER TABLE connectors DROP CONSTRAINT IF EXISTS connectors_config_holds_no_secrets;
DROP FUNCTION IF EXISTS config_names_a_secret(jsonb);

GRANT SELECT ON entities TO hippo_api;
ALTER TABLE actions DROP COLUMN IF EXISTS summary;
