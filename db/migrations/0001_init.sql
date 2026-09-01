-- Placeholder schema: videos + their transcript segments, with a manual
-- review flag so a human can confirm the Whisper output is correct before
-- it's trusted for retrieval. pgvector is enabled up front since the real
-- RAG backend (semantic_search) will need it — no vector columns yet.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS videos (
    id               SERIAL PRIMARY KEY,
    slug             TEXT UNIQUE NOT NULL,
    title            TEXT NOT NULL,
    filename         TEXT NOT NULL,        -- relative to MEDIA_ROOT/raw
    duration_seconds NUMERIC NOT NULL,
    license          TEXT,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id             SERIAL PRIMARY KEY,
    video_id       INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,       -- ordering within the video
    start_ms       INTEGER NOT NULL,
    end_ms         INTEGER NOT NULL,
    text           TEXT NOT NULL,
    review_status  TEXT NOT NULL DEFAULT 'unreviewed'
                   CHECK (review_status IN ('unreviewed', 'correct', 'incorrect')),
    corrected_text TEXT,
    reviewed_at    TIMESTAMPTZ,
    UNIQUE (video_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_transcript_segments_video_start
    ON transcript_segments (video_id, start_ms);
