"""Backfill embeddings for every transcript_segments row that doesn't have
one yet. Run after seeding, before the agent loop can do semantic_search.

    docker compose exec api python -m app.embed_segments
    # or locally: DATABASE_URL=... OPENROUTER_API_KEY=... python -m app.embed_segments
"""

from __future__ import annotations

import asyncio
import logging

from . import queries
from .db import close_pool, get_pool
from .openrouter import embed

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("embed_segments")

BATCH_SIZE = 16


async def main() -> None:
    db = await get_pool()
    total = 0
    while True:
        rows = await queries.segments_missing_embedding(db, limit=BATCH_SIZE)
        if not rows:
            break
        texts = [r["text"] for r in rows]
        vectors = await embed(texts)
        for row, vector in zip(rows, vectors):
            await queries.set_segment_embedding(db, row["id"], vector)
        total += len(rows)
        log.info("embedded %d segments so far", total)
    log.info("done — %d segments embedded", total)
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
