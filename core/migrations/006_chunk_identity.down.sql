-- Reverts 006_chunk_identity.sql.

DROP INDEX IF EXISTS chunks_unembedded_idx;
DROP INDEX IF EXISTS chunks_entity_content_key;
DROP TRIGGER IF EXISTS chunks_set_content_hash ON chunks;
DROP FUNCTION IF EXISTS set_chunk_content_hash();

ALTER TABLE chunks
    DROP COLUMN IF EXISTS content_hash,
    DROP COLUMN IF EXISTS chunk_index;
