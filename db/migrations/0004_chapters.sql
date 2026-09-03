-- Content chaptering (PLAN.md feature priority #2): a coarser, named-scene
-- layer sibling to transcript_segments, for navigation and high-level
-- retrieval. Deliberately its own table with its own embedding column
-- (not a column bolted onto transcript_segments) so transcripts and
-- chapters can be queried separately, per CLAUDE.md — chapters are for
-- outline/navigation, transcript_segments stay the only thing
-- final_answer citations ever point at.
--
-- Same dimension/no-ANN-index reasoning as 0002_search.sql's
-- transcript_segments.embedding: 4096-dim (qwen/qwen3-embedding-8b),
-- exact-scan cosine distance, fine at this corpus size.
CREATE TABLE IF NOT EXISTS chapters (
    id         SERIAL PRIMARY KEY,
    video_id   INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,       -- ordering within the video
    title      TEXT NOT NULL,
    summary    TEXT NOT NULL,
    start_ms   INTEGER NOT NULL,
    end_ms     INTEGER NOT NULL,
    embedding  vector(4096),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (video_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_chapters_video_start
    ON chapters (video_id, start_ms);
