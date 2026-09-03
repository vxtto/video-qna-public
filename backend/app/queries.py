"""All hand-written SQL for this project, in one place, grouped by
resource. No ORM — see CLAUDE.md's ORM decision: the real queries here
(pgvector cosine distance, Postgres full-text ranking, RRF fusion) are
Postgres-specific enough that an ORM would just add ceremony around
`.execute(text(...))` anyway. `db.py` owns the connection pool; this module
owns every statement run against it.

Functions take an acquired `asyncpg` connection or pool (anything with
`.fetch`/`.fetchrow`/`.fetchval`/`.execute`) as their first argument.
"""

from __future__ import annotations

import json
from typing import Any

import asyncpg

# ---------------------------------------------------------------------------
# videos
# ---------------------------------------------------------------------------


async def list_videos(db: asyncpg.Pool) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT v.id, v.slug, v.title, v.filename, v.duration_seconds, v.license,
               count(s.id) AS segment_count,
               count(s.id) FILTER (WHERE s.review_status != 'unreviewed') AS reviewed_count,
               count(s.id) FILTER (WHERE s.embedding IS NOT NULL) AS embedded_count
        FROM videos v
        LEFT JOIN transcript_segments s ON s.video_id = v.id
        GROUP BY v.id
        ORDER BY v.id
        """
    )


async def get_video_by_slug(db: asyncpg.Pool, slug: str) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM videos WHERE slug = $1", slug)


async def get_video_by_id(db: asyncpg.Pool, video_id: int) -> asyncpg.Record | None:
    return await db.fetchrow("SELECT * FROM videos WHERE id = $1", video_id)


async def resolve_video_ref(db: asyncpg.Pool, ref: str) -> int | None:
    """Resolve a movie named in a tool call (`semantic_search`'s/
    `keyword_search`'s/`search_chapters`'s optional `video` arg) to a
    video id — exact slug match first, then a fuzzy title match, so the
    model can say either "tears-of-steel" or "Tears of Steel". Returns
    None if nothing matches (caller surfaces that to the model rather
    than silently searching every video)."""
    row = await db.fetchrow(
        """
        SELECT id FROM videos
        WHERE slug = $1 OR title ILIKE '%' || $1 || '%'
        ORDER BY (slug = $1) DESC
        LIMIT 1
        """,
        ref,
    )
    return row["id"] if row else None


async def list_segments(db: asyncpg.Pool, video_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, seq, start_ms, end_ms, text, review_status, corrected_text
        FROM transcript_segments
        WHERE video_id = $1
        ORDER BY seq
        """,
        video_id,
    )


async def upsert_video(
    db: asyncpg.Pool,
    *,
    slug: str,
    title: str,
    filename: str,
    duration_seconds: float,
    license: str | None,
) -> int:
    return await db.fetchval(
        """
        INSERT INTO videos (slug, title, filename, duration_seconds, license)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (slug) DO UPDATE
            SET title = EXCLUDED.title,
                filename = EXCLUDED.filename,
                duration_seconds = EXCLUDED.duration_seconds,
                license = EXCLUDED.license
        RETURNING id
        """,
        slug,
        title,
        filename,
        duration_seconds,
        license,
    )


async def replace_segments(
    db: asyncpg.Pool, video_id: int, rows: list[tuple[Any, ...]]
) -> None:
    """rows: (video_id, seq, start_ms, end_ms, text) tuples."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "DELETE FROM transcript_segments WHERE video_id = $1", video_id
            )
            await conn.executemany(
                """
                INSERT INTO transcript_segments (video_id, seq, start_ms, end_ms, text)
                VALUES ($1, $2, $3, $4, $5)
                """,
                rows,
            )


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


async def update_segment_review(
    db: asyncpg.Pool, segment_id: int, review_status: str, corrected_text: str | None
) -> asyncpg.Record | None:
    return await db.fetchrow(
        """
        UPDATE transcript_segments
        SET review_status = $2,
            corrected_text = $3,
            reviewed_at = CASE WHEN $2 = 'unreviewed' THEN NULL ELSE now() END
        WHERE id = $1
        RETURNING id, seq, start_ms, end_ms, text, review_status, corrected_text
        """,
        segment_id,
        review_status,
        corrected_text,
    )


# ---------------------------------------------------------------------------
# embeddings
# ---------------------------------------------------------------------------


async def segments_missing_embedding(
    db: asyncpg.Pool, limit: int = 64
) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, coalesce(corrected_text, text) AS text
        FROM transcript_segments
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT $1
        """,
        limit,
    )


async def set_segment_embedding(
    db: asyncpg.Pool, segment_id: int, embedding: list[float]
) -> None:
    # pgvector accepts the "[0.1,0.2,...]" text format via asyncpg's plain
    # string binding — no custom type codec needed for a write-only path.
    await db.execute(
        "UPDATE transcript_segments SET embedding = $2 WHERE id = $1",
        segment_id,
        _vector_literal(embedding),
    )


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


# ---------------------------------------------------------------------------
# retrieval — the three agent tools (CLAUDE.md risk #3)
# ---------------------------------------------------------------------------


async def semantic_search(
    db: asyncpg.Pool,
    query_embedding: list[float],
    *,
    video_id: int | None = None,
    k: int = 8,
) -> list[asyncpg.Record]:
    """Exact nearest-neighbor scan by cosine distance. No ANN index — see
    CLAUDE.md risk #8: at a few hundred chunks, exact scan is instant and
    pgvector's ivfflat/HNSW index cap (2000 dims) wouldn't fit our 4096-dim
    embeddings anyway."""
    return await db.fetch(
        """
        SELECT s.id, s.video_id, s.seq, s.start_ms, s.end_ms,
               coalesce(s.corrected_text, s.text) AS text,
               v.slug AS video_slug, v.title AS video_title,
               (s.embedding <=> $1) AS distance
        FROM transcript_segments s
        JOIN videos v ON v.id = s.video_id
        WHERE s.embedding IS NOT NULL
          AND ($2::int IS NULL OR s.video_id = $2)
        ORDER BY s.embedding <=> $1
        LIMIT $3
        """,
        _vector_literal(query_embedding),
        video_id,
        k,
    )


async def keyword_search(
    db: asyncpg.Pool,
    query: str,
    *,
    video_id: int | None = None,
    k: int = 8,
) -> list[asyncpg.Record]:
    """Postgres full-text search, ranked by ts_rank. websearch_to_tsquery
    tolerates plain user phrasing (quotes, `-word`, `or`) instead of
    requiring tsquery's `&`/`|` syntax."""
    return await db.fetch(
        """
        SELECT s.id, s.video_id, s.seq, s.start_ms, s.end_ms,
               coalesce(s.corrected_text, s.text) AS text,
               v.slug AS video_slug, v.title AS video_title,
               ts_rank(s.tsv, websearch_to_tsquery('english', $1)) AS rank
        FROM transcript_segments s
        JOIN videos v ON v.id = s.video_id
        WHERE s.tsv @@ websearch_to_tsquery('english', $1)
          AND ($2::int IS NULL OR s.video_id = $2)
        ORDER BY rank DESC
        LIMIT $3
        """,
        query,
        video_id,
        k,
    )


async def fetch_window(
    db: asyncpg.Pool,
    video_id: int,
    timestamp_ms: int,
    *,
    before_ms: int = 30_000,
    after_ms: int = 30_000,
) -> list[asyncpg.Record]:
    """Neighboring transcript context around a point in time — for
    "what happens after/around T" questions that a single matched chunk
    can't answer alone."""
    return await db.fetch(
        """
        SELECT s.id, s.video_id, s.seq, s.start_ms, s.end_ms,
               coalesce(s.corrected_text, s.text) AS text,
               v.slug AS video_slug, v.title AS video_title
        FROM transcript_segments s
        JOIN videos v ON v.id = s.video_id
        WHERE s.video_id = $1
          AND s.end_ms >= $2 AND s.start_ms <= $3
        ORDER BY s.seq
        """,
        video_id,
        timestamp_ms - before_ms,
        timestamp_ms + after_ms,
    )


# ---------------------------------------------------------------------------
# chapters (PLAN.md feature priority #2) — a coarser, navigational layer
# sibling to transcript_segments. Queried separately (own table, own
# embedding column) from transcripts: chapters are for high-level
# retrieval/navigation, transcript_segments stay the only thing
# final_answer ever cites. See CLAUDE.md / db/migrations/0004_chapters.sql.
# ---------------------------------------------------------------------------


async def list_chapters(db: asyncpg.Pool, video_id: int) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, video_id, seq, title, summary, start_ms, end_ms
        FROM chapters
        WHERE video_id = $1
        ORDER BY seq
        """,
        video_id,
    )


async def replace_chapters(
    db: asyncpg.Pool, video_id: int, rows: list[tuple[Any, ...]]
) -> None:
    """rows: (video_id, seq, title, summary, start_ms, end_ms) tuples."""
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("DELETE FROM chapters WHERE video_id = $1", video_id)
            await conn.executemany(
                """
                INSERT INTO chapters (video_id, seq, title, summary, start_ms, end_ms)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                rows,
            )


async def chapters_missing_embedding(
    db: asyncpg.Pool, limit: int = 64
) -> list[asyncpg.Record]:
    return await db.fetch(
        """
        SELECT id, title || '. ' || summary AS text
        FROM chapters
        WHERE embedding IS NULL
        ORDER BY id
        LIMIT $1
        """,
        limit,
    )


async def set_chapter_embedding(
    db: asyncpg.Pool, chapter_id: int, embedding: list[float]
) -> None:
    await db.execute(
        "UPDATE chapters SET embedding = $2 WHERE id = $1",
        chapter_id,
        _vector_literal(embedding),
    )


async def search_chapters(
    db: asyncpg.Pool,
    query_embedding: list[float],
    *,
    video_id: int | None = None,
    k: int = 5,
) -> list[asyncpg.Record]:
    """Semantic search over chapter title+summary — high-level/navigational,
    the chapters counterpart to `semantic_search` over transcript_segments.
    Same exact-scan-no-ANN-index reasoning as that function."""
    return await db.fetch(
        """
        SELECT c.id, c.video_id, c.seq, c.title, c.summary, c.start_ms, c.end_ms,
               v.slug AS video_slug, v.title AS video_title,
               (c.embedding <=> $1) AS distance
        FROM chapters c
        JOIN videos v ON v.id = c.video_id
        WHERE c.embedding IS NOT NULL
          AND ($2::int IS NULL OR c.video_id = $2)
        ORDER BY c.embedding <=> $1
        LIMIT $3
        """,
        _vector_literal(query_embedding),
        video_id,
        k,
    )


# ---------------------------------------------------------------------------
# sessions / messages / events (PLAN.md feature priority #6)
# ---------------------------------------------------------------------------


async def create_session(db: asyncpg.Pool, video_id: int | None) -> asyncpg.Record:
    return await db.fetchrow(
        """
        INSERT INTO sessions (video_id)
        VALUES ($1)
        RETURNING id, video_id, created_at, last_active_at
        """,
        video_id,
    )


async def get_session(db: asyncpg.Pool, session_id: str) -> asyncpg.Record | None:
    return await db.fetchrow(
        "SELECT * FROM sessions WHERE id = $1::uuid", session_id
    )


async def list_messages(db: asyncpg.Pool, session_id: str) -> list[asyncpg.Record]:
    """Ordered oldest-first - both for replaying into the agent loop's
    `history` and for rendering a transcript. Left-joins `events` for the
    feedback column since that's the one bit of event state the frontend
    needs per-message; the rest of the event (trace, tokens, latency) is
    internal analytics, not exposed here."""
    return await db.fetch(
        """
        SELECT m.id, m.role, m.content, m.citations, m.created_at,
               e.feedback
        FROM messages m
        LEFT JOIN events e ON e.message_id = m.id
        WHERE m.session_id = $1::uuid
        ORDER BY m.id
        """,
        session_id,
    )


async def record_turn(
    db: asyncpg.Pool,
    *,
    session_id: str,
    video_id: int | None,
    user_message: str,
    answer: str,
    citations: list[dict],
    trace: list[dict],
    usage: dict,
    latency_ms: int,
) -> asyncpg.Record:
    """Persists one /api/chat turn - the user + assistant messages, an
    analytics event, and a bump of the session's last_active_at - all in
    one transaction so a crash mid-turn can't leave a dangling assistant
    message with no event, or vice versa."""
    chunk_ids = sorted(
        {
            r["id"]
            for step in trace
            if step["tool"] != "final_answer" and isinstance(step.get("result"), list)
            for r in step["result"]
            if isinstance(r, dict) and "id" in r
        }
    )
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO messages (session_id, role, content)
                VALUES ($1::uuid, 'user', $2)
                """,
                session_id,
                user_message,
            )
            assistant_msg = await conn.fetchrow(
                """
                INSERT INTO messages (session_id, role, content, citations)
                VALUES ($1::uuid, 'assistant', $2, $3::jsonb)
                RETURNING id
                """,
                session_id,
                answer,
                json.dumps(citations),
            )
            event = await conn.fetchrow(
                """
                INSERT INTO events (
                    session_id, message_id, video_id, retrieved_chunk_ids,
                    tool_calls, prompt_tokens, completion_tokens, total_tokens,
                    latency_ms
                )
                VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6, $7, $8, $9)
                RETURNING id
                """,
                session_id,
                assistant_msg["id"],
                video_id,
                chunk_ids,
                json.dumps(trace),
                usage.get("prompt_tokens"),
                usage.get("completion_tokens"),
                usage.get("total_tokens"),
                latency_ms,
            )
            await conn.execute(
                "UPDATE sessions SET last_active_at = now() WHERE id = $1::uuid",
                session_id,
            )
    return {"message_id": assistant_msg["id"], "event_id": event["id"]}


async def record_failed_turn(db: asyncpg.Pool, *, session_id: str, user_message: str) -> None:
    """Persists just the user's message when the agent loop/model call fails
    before `record_turn` can run - the fix for the "message vanishes
    silently" regression (memory/no-test-suite-openrouter-failure-handling):
    a mid-turn OpenRouter drop used to mean the user's question was never
    saved, even though the user believes they asked something. No assistant
    message/event row is written here - there's no answer to attach one to."""
    await db.execute(
        "INSERT INTO messages (session_id, role, content) VALUES ($1::uuid, 'user', $2)",
        session_id,
        user_message,
    )


async def set_message_feedback(
    db: asyncpg.Pool, message_id: int, feedback: str
) -> asyncpg.Record | None:
    """Feedback lives on `events` (the analytics row), keyed by
    `message_id`, but the frontend addresses it by message id since that's
    what the user is reacting to in the chat transcript."""
    return await db.fetchrow(
        """
        UPDATE events SET feedback = $2
        WHERE message_id = $1
        RETURNING id, message_id, feedback
        """,
        message_id,
        feedback,
    )
