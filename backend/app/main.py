"""Placeholder FastAPI service: watch a movie in the browser and review
whether its Whisper transcript is correct, segment by segment.

Not the real Q&A backend yet (see repo CLAUDE.md) — this is the smallest
thing that lets a human confirm transcript quality before it's trusted for
retrieval.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.responses import Response, StreamingResponse

from . import agent, queries
from .db import close_pool, get_pool
from .seed import main as seed_main

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("app")

MEDIA_ROOT = Path("/data/media")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    count = await pool.fetchval("SELECT count(*) FROM videos")
    if count == 0:
        log.info("videos table empty, running seed...")
        try:
            await seed_main()
        except Exception:
            log.exception("seed failed (placeholder app, continuing without data)")
    yield
    await close_pool()


app = FastAPI(title="video-qna placeholder", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class SegmentReview(BaseModel):
    review_status: str  # "correct" | "incorrect" | "unreviewed"
    corrected_text: str | None = None


@app.get("/api/videos")
async def list_videos():
    pool = await get_pool()
    rows = await queries.list_videos(pool)
    return [dict(r) for r in rows]


@app.get("/api/videos/{slug}")
async def get_video(slug: str):
    pool = await get_pool()
    video = await queries.get_video_by_slug(pool, slug)
    if video is None:
        raise HTTPException(404, "video not found")
    segments = await queries.list_segments(pool, video["id"])
    return {**dict(video), "segments": [dict(s) for s in segments]}


@app.patch("/api/segments/{segment_id}")
async def review_segment(segment_id: int, body: SegmentReview):
    if body.review_status not in ("unreviewed", "correct", "incorrect"):
        raise HTTPException(422, "invalid review_status")
    pool = await get_pool()
    row = await queries.update_segment_review(
        pool, segment_id, body.review_status, body.corrected_text
    )
    if row is None:
        raise HTTPException(404, "segment not found")
    return dict(row)


class ChatRequest(BaseModel):
    message: str
    video_slug: str | None = None
    # Omit to start a new session; pass one back to continue a
    # conversation - see PLAN.md feature priority #6.
    session_id: uuid.UUID | None = None


def _parse_citations(raw: str | None) -> list[dict] | None:
    return json.loads(raw) if raw else None


async def _resolve_session(pool: asyncpg.Pool, body: ChatRequest) -> tuple[str, int | None]:
    """Shared by /api/chat and /api/chat/stream: find-or-create the session
    for this request, return (session_id, video_id)."""
    if body.session_id is not None:
        session = await queries.get_session(pool, str(body.session_id))
        if session is None:
            raise HTTPException(404, "session not found")
        video_id = session["video_id"]
    else:
        video_id = None
        if body.video_slug:
            video = await queries.get_video_by_slug(pool, body.video_slug)
            if video is None:
                raise HTTPException(404, "video not found")
            video_id = video["id"]
        session = await queries.create_session(pool, video_id)
    return str(session["id"]), video_id


@app.post("/api/chat")
async def chat(body: ChatRequest):
    """Wires the bare agent loop (app/agent.py) into the API, now with
    server-side session storage (PLAN.md feature priority #6): every turn
    is persisted as a `messages` row pair + an `events` analytics row, and
    prior turns in the same session are replayed into the agent loop as
    history. Still no trim/summarize policy (priority #5) - the full
    history is replayed every turn.

    One-shot: the client gets nothing until the whole turn (every tool
    call + the final answer) is done. See POST /api/chat/stream below for
    the version that renders tool calls as they happen."""
    pool = await get_pool()
    session_id, video_id = await _resolve_session(pool, body)

    history_rows = await queries.list_messages(pool, session_id)
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    started = time.monotonic()
    try:
        result = await agent.run(pool, body.message, video_id=video_id, history=history)
    except Exception:
        # Regression guard (memory/no-test-suite-openrouter-failure-handling):
        # an OpenRouter transport failure mid-agent-loop (e.g.
        # httpx.RemoteProtocolError, reproduced live) used to propagate as a
        # bare unhandled 500 with a full stack trace, and the user's message
        # was never persisted anywhere. Now: a clean error response, and the
        # message isn't silently lost.
        log.exception("agent.run failed")
        try:
            await queries.record_failed_turn(pool, session_id=session_id, user_message=body.message)
        except Exception:
            log.exception("failed to persist user message after agent failure")
        raise HTTPException(502, "The assistant is temporarily unavailable. Please try again.")
    latency_ms = int((time.monotonic() - started) * 1000)

    persisted = await queries.record_turn(
        pool,
        session_id=session_id,
        video_id=video_id,
        user_message=body.message,
        answer=result.answer,
        citations=result.citations,
        trace=result.trace,
        usage=result.usage,
        latency_ms=latency_ms,
    )

    return {
        "session_id": session_id,
        "message_id": persisted["message_id"],
        "answer": result.answer,
        "citations": result.citations,
        "trace": result.trace,
    }


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@app.post("/api/chat/stream")
async def chat_stream(body: ChatRequest):
    """SSE twin of /api/chat: same session/history/persistence plumbing
    (via `_resolve_session`, shared with /api/chat), but driven through
    `agent.run_stream` and forwarded to the client as Server-Sent Events -
    so the answer renders token-by-token AND each tool call renders the
    moment it starts/finishes, instead of the client waiting for the whole
    turn to land at once behind a static "...thinking" placeholder.
    /api/chat stays as-is (used by app/cli.py and anything that just wants
    the plain JSON result).

    Event types, in order: one `session` event, then zero or more
    `tool_call`/`tool_result`/`delta` events interleaved in whatever order
    the agent loop actually produced them, followed by exactly one of
    `final` or `error`:
      - `session`     {session_id}
      - `tool_call`   {call_id, tool, args}     - about to dispatch
      - `tool_result` {call_id, tool, result}   - dispatch done (or
                                                   {"error": ...} if it raised)
      - `delta`       {text}                    - answer text grew
      - `final`       {message_id, answer, citations, trace}
      - `error`       {error}

    Session lookup/creation happens before streaming starts (so a bad
    session_id still 404s normally); persistence happens after the agent
    loop finishes, right before the `final` event.
    """
    pool = await get_pool()
    session_id, video_id = await _resolve_session(pool, body)

    history_rows = await queries.list_messages(pool, session_id)
    history = [{"role": r["role"], "content": r["content"]} for r in history_rows]

    async def event_stream():
        started = time.monotonic()
        yield _sse("session", {"session_id": session_id})

        answer = ""
        citations: list[dict] = []
        trace: list[dict] = []
        usage: dict = {}
        try:
            async for event in agent.run_stream(
                pool, body.message, video_id=video_id, history=history
            ):
                if event["type"] == "answer_delta":
                    answer += event["text"]
                    yield _sse("delta", {"text": event["text"]})
                elif event["type"] == "tool_call":
                    yield _sse(
                        "tool_call",
                        {"call_id": event["call_id"], "tool": event["tool"], "args": event["args"]},
                    )
                elif event["type"] == "tool_result":
                    yield _sse(
                        "tool_result",
                        {"call_id": event["call_id"], "tool": event["tool"], "result": event["result"]},
                    )
                elif event["type"] == "done":
                    # Authoritative - overrides whatever the deltas above
                    # streamed, in case partial-JSON decoding drifted.
                    answer = event["answer"]
                    citations = event["citations"]
                    trace = event["trace"]
                    usage = event["usage"]
        except Exception as exc:  # noqa: BLE001 - surface to the client, not a 500 mid-stream
            log.exception("chat stream failed")
            # Regression guard (memory/no-test-suite-openrouter-failure-handling):
            # record_turn only runs after the try block succeeds, so a
            # mid-stream OpenRouter drop used to mean the user's message was
            # never persisted - silently vanishing from history even though
            # the user believes they asked something. Save at least that much.
            try:
                await queries.record_failed_turn(pool, session_id=session_id, user_message=body.message)
            except Exception:
                log.exception("failed to persist user message after stream failure")
            yield _sse("error", {"error": str(exc)})
            return

        latency_ms = int((time.monotonic() - started) * 1000)
        persisted = await queries.record_turn(
            pool,
            session_id=session_id,
            video_id=video_id,
            user_message=body.message,
            answer=answer,
            citations=citations,
            trace=trace,
            usage=usage,
            latency_ms=latency_ms,
        )
        yield _sse(
            "final",
            {
                "message_id": persisted["message_id"],
                "answer": answer,
                "citations": citations,
                "trace": trace,
            },
        )

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            # nginx/proxy buffering would otherwise batch chunks and defeat
            # the whole point (see DEPLOY.md for what fronts this in prod).
            "x-accel-buffering": "no",
        },
    )


@app.get("/api/sessions/{session_id}")
async def get_session_transcript(session_id: str):
    pool = await get_pool()
    session = await queries.get_session(pool, session_id)
    if session is None:
        raise HTTPException(404, "session not found")
    messages = await queries.list_messages(pool, session_id)
    return {
        "id": str(session["id"]),
        "video_id": session["video_id"],
        "created_at": session["created_at"],
        "last_active_at": session["last_active_at"],
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "citations": _parse_citations(m["citations"]),
                "created_at": m["created_at"],
                "feedback": m["feedback"],
            }
            for m in messages
        ],
    }


class FeedbackRequest(BaseModel):
    feedback: str  # "up" | "down"


@app.patch("/api/messages/{message_id}/feedback")
async def set_message_feedback(message_id: int, body: FeedbackRequest):
    if body.feedback not in ("up", "down"):
        raise HTTPException(422, "feedback must be 'up' or 'down'")
    pool = await get_pool()
    row = await queries.set_message_feedback(pool, message_id, body.feedback)
    if row is None:
        raise HTTPException(404, "message not found (or has no associated event)")
    return dict(row)


CHUNK_SIZE = 1024 * 1024  # 1MB


@app.get("/media/{filename}")
async def stream_media(filename: str, request: Request):
    """Serve a raw video file with HTTP Range support.

    Starlette's StaticFiles/FileResponse does NOT implement Range requests
    (confirmed against starlette 0.38 — it always returns the full body
    with a 200), and per CLAUDE.md risk #7 seeking breaks without it. So
    this is hand-rolled: a bad Range header degrades to a full 200 response
    rather than erroring, since some HTTP clients omit Range entirely.
    """
    path = MEDIA_ROOT / "raw" / filename
    if not path.is_file():
        raise HTTPException(404, "file not found")

    file_size = path.stat().st_size
    range_header = request.headers.get("range")

    if range_header is None:
        return StreamingResponse(
            _iterfile(path, 0, file_size - 1),
            media_type="video/mp4",
            headers={"accept-ranges": "bytes", "content-length": str(file_size)},
        )

    try:
        units, _, range_spec = range_header.partition("=")
        start_s, _, end_s = range_spec.partition("-")
        start = int(start_s) if start_s else 0
        end = int(end_s) if end_s else file_size - 1
        end = min(end, file_size - 1)
        if units != "bytes" or start > end:
            raise ValueError
    except ValueError:
        return Response(status_code=416, headers={"content-range": f"bytes */{file_size}"})

    return StreamingResponse(
        _iterfile(path, start, end),
        status_code=206,
        media_type="video/mp4",
        headers={
            "accept-ranges": "bytes",
            "content-range": f"bytes {start}-{end}/{file_size}",
            "content-length": str(end - start + 1),
        },
    )


def _iterfile(path: Path, start: int, end: int):
    with open(path, "rb") as f:
        f.seek(start)
        remaining = end - start + 1
        while remaining > 0:
            chunk = f.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

# Frontend placeholder — plain HTML/JS, no build step (see CLAUDE.md: never
# run a Node dev server in production; this keeps dev/prod identical).
app.mount("/", StaticFiles(directory="static", html=True), name="static")
