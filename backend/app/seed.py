"""Load the transcription output (data/media/raw/*.mp4 +
data/media/transcripts/*.json) into Postgres.

Each transcript chunk file
covers CHUNK_SECONDS of the video starting at chunk_index * CHUNK_SECONDS;
segment timestamps inside a chunk are relative to the chunk, so we offset
them to get a timeline for the whole video.

Extend MANIFEST as more movies come out of the pipeline.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path

from . import queries
from .db import get_pool

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("seed")

MEDIA_ROOT = Path("/data/media")
CHUNK_SECONDS = 120

MANIFEST = [
    {
        "slug": "tears-of-steel",
        "title": "Tears of Steel",
        "filename": "tears_of_steel_480p.mp4",
        "license": "CC-BY 3.0",
        "transcript_prefix": "tos",
    },
    {
        "slug": "cosmos-laundromat",
        "title": "Cosmos Laundromat",
        "filename": "cosmos_laundromat_480p.mp4",
        "license": "CC-BY-SA 4.0",
        "transcript_prefix": "cos",
    },
]

CHUNK_RE = re.compile(r"_(\d+)\.json$")


def load_chunks(prefix: str) -> list[dict]:
    """Return transcript chunks for `prefix`, in chunk-index order."""
    files = sorted(
        MEDIA_ROOT.glob(f"transcripts/{prefix}_*.json"),
        key=lambda p: int(CHUNK_RE.search(p.name).group(1)),
    )
    return [json.loads(f.read_text()) for f in files]


async def seed_video(pool, entry: dict) -> None:
    chunks = load_chunks(entry["transcript_prefix"])
    if not chunks:
        log.warning("no transcript chunks found for %s, skipping", entry["slug"])
        return

    duration_seconds = sum(c["duration"] for c in chunks)

    video_id = await queries.upsert_video(
        pool,
        slug=entry["slug"],
        title=entry["title"],
        filename=entry["filename"],
        duration_seconds=duration_seconds,
        license=entry["license"],
    )

    seq = 0
    rows = []
    for chunk_index, chunk in enumerate(chunks):
        offset_ms = round(chunk_index * CHUNK_SECONDS * 1000)
        for segment in chunk["segments"]:
            rows.append(
                (
                    video_id,
                    seq,
                    offset_ms + round(segment["start"] * 1000),
                    offset_ms + round(segment["end"] * 1000),
                    segment["text"].strip(),
                )
            )
            seq += 1

    await queries.replace_segments(pool, video_id, rows)
    log.info("seeded %s: %d segments (%.1fs)", entry["slug"], len(rows), duration_seconds)


async def main() -> None:
    pool = await get_pool()
    for entry in MANIFEST:
        video_path = MEDIA_ROOT / "raw" / entry["filename"]
        if not video_path.exists():
            log.warning(
                "video file missing (%s) — is data/media mounted at %s?",
                video_path,
                MEDIA_ROOT,
            )
            continue
        await seed_video(pool, entry)


if __name__ == "__main__":
    asyncio.run(main())
