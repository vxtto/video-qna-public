-- Adds what the real RAG retrieval path needs: a vector column for
-- semantic_search and a tsvector column for keyword_search. See
-- CLAUDE.md's retrieval section (hybrid search, RRF fusion).
--
-- Dimension: 4096, matching qwen/qwen3-embedding-8b's native output. No
-- ivfflat/HNSW index — per CLAUDE.md risk #8 we skip ANN indexing at this
-- corpus size (few hundred chunks, exact scan is instant); pgvector's
-- 2000-dim index cap wouldn't fit 4096 anyway, which is one more reason
-- exact scan is the right call here, not just corpus size.
ALTER TABLE transcript_segments
    ADD COLUMN IF NOT EXISTS embedding vector(4096);

-- Generated column so it can never drift from `text`/`corrected_text`;
-- prefer the human-corrected text once a segment has been reviewed.
ALTER TABLE transcript_segments
    ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(corrected_text, text))
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_transcript_segments_tsv
    ON transcript_segments USING GIN (tsv);
