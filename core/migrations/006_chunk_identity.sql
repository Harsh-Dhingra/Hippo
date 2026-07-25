-- ============================================================
-- Hippo — Content-addressed chunks (P1-RES-3)
--
-- Enrichment is re-run whenever a chunking policy changes, an embedding model
-- is swapped, or a summary needs regenerating. STACK.md is explicit that
-- re-embedding is a resolver re-run by design, so a re-run has to be cheap and
-- has to be safe.
--
-- Identifying a chunk by the hash of its content is what makes it both. A
-- re-run recomputes the same hashes for unchanged text, so those rows are
-- untouched and keep the embedding they already have; only genuinely changed
-- text costs an embedding call. And because (entity_id, content_hash) is
-- unique, a second run cannot append a duplicate copy of a chunk it already
-- wrote, which is the done-condition for this fragment.
--
-- The hash is computed by a trigger rather than trusted from the writer. A
-- content hash that does not match its content would silently break both
-- properties above, and the guarantee is worth more than the microsecond. A
-- generated column would be better still, but the expression needs an encoding
-- conversion that Postgres does not consider immutable.
-- ============================================================

ALTER TABLE chunks
    ADD COLUMN content_hash text,
    ADD COLUMN chunk_index  int NOT NULL DEFAULT 0;

CREATE FUNCTION set_chunk_content_hash() RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog, public
AS $$
BEGIN
    NEW.content_hash := encode(sha256(convert_to(NEW.content, 'UTF8')), 'hex');
    RETURN NEW;
END;
$$;
REVOKE ALL ON FUNCTION set_chunk_content_hash() FROM PUBLIC;

CREATE TRIGGER chunks_set_content_hash
    BEFORE INSERT OR UPDATE OF content ON chunks
    FOR EACH ROW EXECUTE FUNCTION set_chunk_content_hash();

-- Backfill anything written before this migration.
UPDATE chunks
SET content_hash = encode(sha256(convert_to(content, 'UTF8')), 'hex')
WHERE content_hash IS NULL;

ALTER TABLE chunks ALTER COLUMN content_hash SET NOT NULL;

-- Two chunks of one entity with identical text are one chunk. That is correct
-- rather than merely convenient: a ticket whose summary repeats its
-- description should be embedded and retrieved once.
CREATE UNIQUE INDEX chunks_entity_content_key ON chunks (entity_id, content_hash);

-- The enrichment pass asks "what still needs embedding" on every run.
CREATE INDEX chunks_unembedded_idx ON chunks (entity_id) WHERE embedding IS NULL;

COMMENT ON COLUMN chunks.content_hash IS
    'sha256 of content, hex, maintained by trigger. Chunk identity within an '
    'entity, so a re-run preserves embeddings for text that has not changed.';
COMMENT ON COLUMN chunks.chunk_index IS
    'Order within the entity. Reading order, not identity.';
