"""Local REPL for testing the agent loop end-to-end against a running
Postgres (docker compose up db) before wiring it into the FastAPI app or
touching the VPS at all.

    docker compose up -d db
    docker compose exec api python -m app.embed_segments   # once, after seeding
    DATABASE_URL=postgresql://postgres:postgres@localhost:5432/video_qna \\
        OPENROUTER_API_KEY=... python -m app.cli tears-of-steel
"""

from __future__ import annotations

import asyncio
import sys

from . import agent, queries
from .db import close_pool, get_pool


async def main() -> None:
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    db = await get_pool()

    video_id = None
    if slug:
        video = await queries.get_video_by_slug(db, slug)
        if video is None:
            print(f"no video with slug {slug!r} - listing all videos instead")
        else:
            video_id = video["id"]
            print(f"scoped to video: {video['title']} (id={video_id})")

    print("Agent loop REPL. Empty line or Ctrl-D to quit.\n")
    history: list[dict] = []

    try:
        while True:
            try:
                question = input("> ").strip()
            except EOFError:
                break
            if not question:
                break

            result = await agent.run(db, question, video_id=video_id, history=history)

            for step in result.trace:
                args_preview = str(step["args"])[:120]
                if step["tool"] == "final_answer":
                    print(f"  [final_answer] {args_preview}")
                    continue
                result_preview = str(step["result"])[:200]
                print(f"  [{step['tool']}] args={args_preview} -> {result_preview}")

            print(f"\n{result.answer}\n")
            for c in result.citations:
                mm, ss = divmod(c["start_ms"] // 1000, 60)
                print(f"  [{mm:02d}:{ss:02d}] chunk_id={c['chunk_id']}")
            print()

            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": result.answer})
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
