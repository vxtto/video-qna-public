"""Derive named chapters for every video that has transcript segments but
no chapters yet, then embed them. Run after `app.seed` (needs segments)
and can run before or after `app.embed_segments` — independent tables.

    docker compose exec api python -m app.generate_chapters
    # or locally: DATABASE_URL=... OPENROUTER_API_KEY=... python -m app.generate_chapters

Source: one LLM pass over each video's full transcript (PLAN.md feature
priority #2 — cheap, same DeepSeek call pattern the agent loop already
uses; a 2hr transcript is ~10-25k tokens per PLAN.md risk #1, trivially
fits in one call, no chunking needed at this corpus size). Deliberately
NOT scene-detection on the raw video — overkill for talking-heads content.

The model is asked to anchor chapter boundaries to transcript segment
`seq` numbers, not raw milliseconds — LLMs are unreliable at emitting
exact timestamps, but segment sequence numbers are small integers it can
count against the numbered list in the prompt, and we convert seq back to
real start_ms/end_ms from the segments we already fetched. This means a
chapter boundary always lands exactly on a real segment boundary, never a
hallucinated timestamp.
"""

from __future__ import annotations

import asyncio
import json
import logging

from . import queries
from .db import close_pool, get_pool
from .openrouter import chat, embed

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("generate_chapters")

EMBED_BATCH_SIZE = 16

CHAPTER_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "chapters",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "chapters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "summary": {"type": "string"},
                            "start_seq": {"type": "integer"},
                            "end_seq": {"type": "integer"},
                        },
                        "required": ["title", "summary", "start_seq", "end_seq"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["chapters"],
            "additionalProperties": False,
        },
    },
}

SYSTEM_PROMPT = """\
You split a movie transcript into named chapters (topic/scene-level, \
coarser than individual transcript lines) for a video player's chapter \
index and for high-level/thematic search.

You will be given the full transcript as a numbered list of segments \
("seq: text"). Group consecutive segments into 4-10 chapters covering the \
whole transcript with no gaps and no overlaps - the first chapter's \
start_seq must be the lowest seq in the transcript, the last chapter's \
end_seq must be the highest, and each next chapter's start_seq must be \
exactly the previous chapter's end_seq + 1. Give each chapter a short, \
concrete title (not "Chapter 1") and a 1-2 sentence summary of what \
happens in it. Reference only seq numbers that appear in the transcript.
"""


def _build_prompt(segments: list) -> str:
    lines = [f"{s['seq']}: {s['text']}" for s in segments]
    return "\n".join(lines)


def _rows_from_llm_chapters(
    video_id: int, llm_chapters: list[dict], segments_by_seq: dict[int, dict]
) -> list[tuple]:
    rows = []
    for seq, ch in enumerate(llm_chapters):
        start_seg = segments_by_seq.get(ch["start_seq"])
        end_seg = segments_by_seq.get(ch["end_seq"])
        if start_seg is None or end_seg is None or ch["end_seq"] < ch["start_seq"]:
            log.warning(
                "skipping chapter %r - bad seq range (start=%s end=%s)",
                ch.get("title"),
                ch.get("start_seq"),
                ch.get("end_seq"),
            )
            continue
        rows.append(
            (
                video_id,
                seq,
                ch["title"].strip(),
                ch["summary"].strip(),
                start_seg["start_ms"],
                end_seg["end_ms"],
            )
        )
    return rows


async def generate_for_video(db, video: dict) -> None:
    segments = await queries.list_segments(db, video["id"])
    if not segments:
        log.warning("no segments for %s, skipping", video["slug"])
        return

    segments_by_seq = {s["seq"]: s for s in segments}
    result = await chat(
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_prompt(segments)},
        ],
        response_format=CHAPTER_SCHEMA,
    )
    try:
        parsed = json.loads(result["message"]["content"])
        llm_chapters = parsed["chapters"]
    except (KeyError, json.JSONDecodeError, TypeError):
        log.exception("bad chapter response for %s, skipping", video["slug"])
        return

    rows = _rows_from_llm_chapters(video["id"], llm_chapters, segments_by_seq)
    if not rows:
        log.warning("no usable chapters produced for %s, skipping", video["slug"])
        return

    await queries.replace_chapters(db, video["id"], rows)
    log.info("generated %d chapters for %s", len(rows), video["slug"])


async def embed_pending_chapters(db) -> None:
    total = 0
    while True:
        rows = await queries.chapters_missing_embedding(db, limit=EMBED_BATCH_SIZE)
        if not rows:
            break
        vectors = await embed([r["text"] for r in rows])
        for row, vector in zip(rows, vectors):
            await queries.set_chapter_embedding(db, row["id"], vector)
        total += len(rows)
    if total:
        log.info("embedded %d chapters", total)


async def main() -> None:
    db = await get_pool()
    videos = await queries.list_videos(db)
    for video in videos:
        existing = await queries.list_chapters(db, video["id"])
        if existing:
            log.info("%s already has %d chapters, skipping generation", video["slug"], len(existing))
            continue
        await generate_for_video(db, dict(video))
    await embed_pending_chapters(db)
    log.info("done")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
