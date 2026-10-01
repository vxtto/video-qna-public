"""The agent's tools (several real tools, not one, so
tool choice is genuinely load-bearing — who-says-X routes to keyword,
thematic questions route to semantic, what-happens-after-T routes to
fetch_window, high-level/outline questions route to search_chapters).
Also the `final_answer` tool that structurally enforces citations:
the model must call it to finish, it can't just emit free text
with timestamps sprinkled in.

Transcript-facing tools (semantic_search, keyword_search, fetch_window)
are the grounding layer — every final_answer citation must point at a
transcript_segments row one of these returned. search_chapters is a
separate, coarser layer for navigation and
high-level retrieval only ("what's this act about") — it is never itself
a citation source.

All four search-ish tools take an optional `video` arg (slug or title) so
a call can be scoped to one movie even in a session that isn't already
locked to a single video (queries.resolve_video_ref) — without it, a
session with no bound video would search across every movie's rows.
"""

from __future__ import annotations

from typing import Any

import asyncpg

from . import queries
from .openrouter import embed

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "semantic_search",
            "description": (
                "Search transcript chunks by meaning/theme, not exact wording, to find "
                "the exact segments to ground and cite an answer with. Best for 'what is "
                "this movie about', 'when do they talk about X theme' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search query."},
                    "k": {"type": "integer", "description": "Max results (default 8)."},
                    "video": {
                        "type": "string",
                        "description": (
                            "Movie slug or title to restrict the search to. Pass this "
                            "whenever the user names a specific movie and the conversation "
                            "isn't already scoped to a single one - otherwise this searches "
                            "every movie's transcript."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "keyword_search",
            "description": (
                "Search transcript chunks for exact words/phrases (names, quotes, "
                "specific terms), to find the exact segments to ground and cite an "
                "answer with. Best for 'who says X', 'find the line about Y'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Exact word(s)/phrase to search for."},
                    "k": {"type": "integer", "description": "Max results (default 8)."},
                    "video": {
                        "type": "string",
                        "description": (
                            "Movie slug or title to restrict the search to. Pass this "
                            "whenever the user names a specific movie and the conversation "
                            "isn't already scoped to a single one - otherwise this searches "
                            "every movie's transcript."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_window",
            "description": (
                "Fetch transcript segments around a specific timestamp in a specific "
                "video, to ground and cite more surrounding context — for 'what happens "
                "right after/before T' questions, or to expand a chunk found by search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "video_id": {"type": "integer", "description": "Video ID (from a prior search result)."},
                    "timestamp_ms": {"type": "integer", "description": "Center point, milliseconds."},
                    "before_ms": {"type": "integer", "description": "Milliseconds before (default 30000)."},
                    "after_ms": {"type": "integer", "description": "Milliseconds after (default 30000)."},
                },
                "required": ["video_id", "timestamp_ms"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_chapters",
            "description": (
                "Search a movie's named chapters (coarser than transcript segments - "
                "topic/scene-level) by meaning, for structure/outline/navigation "
                "questions like 'what's the second act about' or 'which part covers X'. "
                "Navigational only, NOT a citation source - once you know which chapter "
                "is relevant, follow up with semantic_search/keyword_search + "
                "fetch_window over that time range to find the actual segments to cite."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language topic/query."},
                    "k": {"type": "integer", "description": "Max results (default 5)."},
                    "video": {
                        "type": "string",
                        "description": (
                            "Movie slug or title to restrict the search to. Pass this "
                            "whenever the user names a specific movie and the conversation "
                            "isn't already scoped to a single one - otherwise this searches "
                            "every movie's chapters."
                        ),
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "final_answer",
            "description": (
                "Finish the turn. ALWAYS call this to deliver your answer instead of "
                "replying with plain text — citations must be structured, not typed "
                "into prose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "answer": {"type": "string", "description": "The answer to the user's question."},
                    "citations": {
                        "type": "array",
                        "description": "Transcript segments the answer is grounded in.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "chunk_id": {"type": "integer"},
                                "video_id": {"type": "integer"},
                                "start_ms": {"type": "integer"},
                            },
                            "required": ["chunk_id", "video_id", "start_ms"],
                        },
                    },
                },
                "required": ["answer", "citations"],
            },
        },
    },
]


def _row_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    d = dict(r)
    if "distance" in d:
        d["distance"] = round(float(d["distance"]), 4)
    if "rank" in d:
        d["rank"] = round(float(d["rank"]), 4)
    return d


async def _resolve_scope(
    db: asyncpg.Pool, args: dict, *, default_video_id: int | None
) -> tuple[int | None, dict | None]:
    """Resolve a call's optional `video` arg (slug/title) against
    queries.resolve_video_ref, falling back to the session's
    default_video_id when the arg is absent. Returns (video_id, error) -
    error is a dict to return as the tool result immediately if the named
    movie doesn't resolve (never silently falls back to searching
    everything just because a name was given but not found)."""
    ref = args.get("video")
    if not ref:
        return default_video_id, None
    video_id = await queries.resolve_video_ref(db, ref)
    if video_id is None:
        return None, {"error": f"unknown movie: {ref!r}"}
    return video_id, None


async def dispatch(
    db: asyncpg.Pool, name: str, args: dict, *, default_video_id: int | None
) -> Any:
    """Run one tool call, return a JSON-serializable result. `final_answer`
    is handled by the caller (agent.py), never dispatched here."""
    if name == "semantic_search":
        video_id, error = await _resolve_scope(db, args, default_video_id=default_video_id)
        if error:
            return error
        [vector] = await embed([args["query"]])
        rows = await queries.semantic_search(
            db, vector, video_id=video_id, k=args.get("k", 8)
        )
        return [_row_to_dict(r) for r in rows]

    if name == "keyword_search":
        video_id, error = await _resolve_scope(db, args, default_video_id=default_video_id)
        if error:
            return error
        rows = await queries.keyword_search(
            db, args["query"], video_id=video_id, k=args.get("k", 8)
        )
        return [_row_to_dict(r) for r in rows]

    if name == "fetch_window":
        rows = await queries.fetch_window(
            db,
            args["video_id"],
            args["timestamp_ms"],
            before_ms=args.get("before_ms", 30_000),
            after_ms=args.get("after_ms", 30_000),
        )
        return [_row_to_dict(r) for r in rows]

    if name == "search_chapters":
        video_id, error = await _resolve_scope(db, args, default_video_id=default_video_id)
        if error:
            return error
        [vector] = await embed([args["query"]])
        rows = await queries.search_chapters(
            db, vector, video_id=video_id, k=args.get("k", 5)
        )
        return [_row_to_dict(r) for r in rows]

    raise ValueError(f"unknown tool: {name}")
