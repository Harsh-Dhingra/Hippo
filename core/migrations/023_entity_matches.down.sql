-- Reverses 023_entity_matches.sql.
--
-- The model edges go with it. They are inferences, reconstructible by running
-- the matcher again, and leaving them behind would strand rows whose reasoning
-- had just been deleted.

DELETE FROM edges WHERE provenance = 'model';

DROP FUNCTION IF EXISTS forget_model_inferences();
DROP FUNCTION IF EXISTS my_entity_matches(uuid, int);
DROP TABLE IF EXISTS entity_matches;
