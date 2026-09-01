"""The agent's tools (CLAUDE.md risk #3: three real tools, not one, so
tool choice is genuinely load-bearing — who-says-X routes to keyword,
thematic questions route to semantic, what-happens-after-T routes to
fetch_window). Also the `final_answer` tool that structurally enforces
citations (risk #7): the model must call it to finish, it can't just emit
free text with timestamps sprinkled in.
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
                "Search transcript chunks by meaning/theme, not exact wording. "
                "Best for 'what is this movie about', 'when do they talk about "
                "X theme' style questions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Natural-language search query."},
                    "k": {"type": "integer", "description": "Max results (default 8)."},
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
                "specific terms). Best for 'who says X', 'find the line about Y'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Exact word(s)/phrase to search for."},
                    "k": {"type": "integer", "description": "Max results (default 8)."},
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
                "video — for 'what happens right after/before T' questions, or to get "
                "more surrounding context for a chunk found by search."
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


async def dispatch(
    db: asyncpg.Pool, name: str, args: dict, *, default_video_id: int | None
) -> Any:
    """Run one tool call, return a JSON-serializable result. `final_answer`
    is handled by the caller (agent.py), never dispatched here."""
    if name == "semantic_search":
        [vector] = await embed([args["query"]])
        rows = await queries.semantic_search(
            db, vector, video_id=default_video_id, k=args.get("k", 8)
        )
        return [_row_to_dict(r) for r in rows]

    if name == "keyword_search":
        rows = await queries.keyword_search(
            db, args["query"], video_id=default_video_id, k=args.get("k", 8)
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

    raise ValueError(f"unknown tool: {name}")
