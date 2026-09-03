"""Shared fixtures for the whole suite.

Env vars must be set *before* anything under `app/` is imported — several
modules read them at import time (`app/db.py`'s DATABASE_URL,
`app/openrouter.py`'s API_KEY/_HEADERS) — so this happens at module load,
above every other import in this file. conftest.py is always imported by
pytest before any test module, so this ordering is safe.
"""

import os

os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/video_qna"
)
os.environ.setdefault("OPENROUTER_API_KEY", "test-dummy-key-not-real")

import asyncio  # noqa: E402
import json  # noqa: E402
import pathlib  # noqa: E402

import asyncpg  # noqa: E402
import pytest  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import queries  # noqa: E402
from app.db import DATABASE_URL  # noqa: E402
from app.main import app  # noqa: E402

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "db" / "migrations"

# Tables truncated between tests, in an order CASCADE would handle anyway —
# spelled out for clarity about what state a test can rely on being empty.
APP_TABLES = "events, messages, sessions, transcript_segments, videos"


@pytest.fixture(scope="session", autouse=True)
def _migrated_schema():
    """Apply db/migrations/*.sql, in filename order, once per test session —
    the same migrations the `migrations` CI job and prod both run, so tests
    exercise the real schema rather than a hand-maintained duplicate of it."""

    async def _run():
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                await conn.execute(path.read_text())
        finally:
            await conn.close()

    asyncio.run(_run())


@pytest.fixture(autouse=True)
def _clean_db():
    """Every test starts from an empty DB. Cheap at this table count/size —
    a per-test transaction-rollback fixture would be faster but doesn't
    compose with the app's own pool doing its own commits mid-test."""

    async def _truncate():
        conn = await asyncpg.connect(DATABASE_URL)
        try:
            await conn.execute(f"TRUNCATE {APP_TABLES} RESTART IDENTITY CASCADE")
        finally:
            await conn.close()

    asyncio.run(_truncate())
    yield


@pytest.fixture
def client(monkeypatch):
    """A TestClient that actually runs the app's lifespan (startup/shutdown
    hooks - notably `get_pool()`/`close_pool()`), same as a real request.

    The lifespan auto-seeds from real movie files (`/data/media`, gitignored,
    never present in a test environment) whenever `videos` is empty - true
    for every test here after `_clean_db` truncates it. Stub that out: it'd
    otherwise fail (silently, since main.py's lifespan already swallows the
    exception) on every single test for no benefit - tests that need a video
    row seed one explicitly via the `seeded_video` fixture instead."""
    import app.main as main_module

    async def _no_seed():
        return None

    monkeypatch.setattr(main_module, "seed_main", _no_seed)
    with TestClient(app) as c:
        yield c


@pytest.fixture
async def seeded_video() -> int:
    """One video + two transcript segments, inserted directly via
    `app.queries` (there's no API to create a video - `seed.py` only ever
    populates one from real media files). Returns the video's id."""
    # A real Pool, not a bare Connection - queries.replace_segments does its
    # own `db.acquire()` (needs a transaction across a DELETE+executemany),
    # which only a Pool exposes.
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=1)
    try:
        video_id = await queries.upsert_video(
            pool,
            slug="demo",
            title="Demo Video",
            filename="sample_clip.mp4",
            duration_seconds=6,
            license="CC0 (synthetic test fixture)",
        )
        await queries.replace_segments(
            pool,
            video_id,
            rows=[
                (video_id, 1, 0, 5000, "hello there"),
                (video_id, 2, 5000, 10000, "general kenobi"),
            ],
        )
        return video_id
    finally:
        await pool.close()


def final_answer_response(answer: str = "hello", citations: list[dict] | None = None) -> dict:
    """Shape of `openrouter.chat()`'s return value when the model calls
    `final_answer` on its first turn with no other tool calls — the
    minimal canned response needed to drive /api/chat's happy path without
    a real LLM call."""
    return {
        "message": {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "final_answer",
                        "arguments": json.dumps({"answer": answer, "citations": citations or []}),
                    },
                }
            ],
        },
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


async def final_answer_stream_chunks(answer: str = "hello", citations: list[dict] | None = None):
    """Shape of `openrouter.chat_stream()`'s yielded chunks for the same
    canned scenario as `final_answer_response`, but as the streaming/delta
    chunks `agent.run_stream` expects (one tool-call delta, then a
    trailing usage-only chunk)."""
    args = json.dumps({"answer": answer, "citations": citations or []})
    yield {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {"name": "final_answer", "arguments": args},
                        }
                    ]
                }
            }
        ]
    }
    yield {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
