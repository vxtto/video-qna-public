-- Server-side chat sessions. Until now
-- /api/chat was one-shot per call with no history and no analytics trail;
-- this adds three tables: sessions, messages,
-- events. gen_random_uuid() is built into Postgres core since PG13 (moved
-- out of pgcrypto) - no extension needed, unlike the `vector` extension in
-- 0001_init.sql.

CREATE TABLE IF NOT EXISTS sessions (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    video_id       INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Conversation turns, verbatim. No trim/summarize policy yet - the whole history for a session is
-- replayed into the agent loop's `messages` array on every turn.
CREATE TABLE IF NOT EXISTS messages (
    id         SERIAL PRIMARY KEY,
    session_id UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    citations  JSONB,              -- only ever set on assistant messages
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages (session_id, id);

-- One row per /api/chat call (i.e. per assistant message) - the analytics
-- trail: retrieved chunk ids (retrieval
-- hit-rate), the full tool-call trace, token counts, latency, and a spot
-- for thumbs up/down once the frontend has a way to send it.
CREATE TABLE IF NOT EXISTS events (
    id                  SERIAL PRIMARY KEY,
    session_id          UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    message_id          INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    video_id            INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    retrieved_chunk_ids INTEGER[] NOT NULL DEFAULT '{}',
    tool_calls          JSONB NOT NULL DEFAULT '[]',
    prompt_tokens       INTEGER,
    completion_tokens   INTEGER,
    total_tokens        INTEGER,
    latency_ms          INTEGER NOT NULL,
    feedback            TEXT CHECK (feedback IN ('up', 'down')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_message ON events (message_id);
