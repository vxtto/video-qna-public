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
