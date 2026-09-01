"""Hand-rolled Hermes-style agent loop (CLAUDE.md: modeled on Nous
Research's Hermes harness, core loop only — model call -> tool dispatch ->
append result -> repeat until final response, no skills system / sub-agents
/ scheduling).

Deliberately small and linear so it's easy to explain in an interview:
this whole module is the agent, no framework underneath.
"""

from __future__ import annotations

import json
import logging
from typing import Awaitable, Callable

import asyncpg

from . import queries, tools
from .openrouter import chat

log = logging.getLogger("agent")

# Called with one event dict per tool call, in real time, so a caller (the
# /api/chat/stream SSE endpoint) can push them to the frontend as they
# happen instead of only after the whole turn finishes. See
# PLAN.md/feature "tool calls rendered in real time". Optional - the
# one-shot /api/chat endpoint and app/cli.py just don't pass one.
EmitFn = Callable[[dict], Awaitable[None]]

MAX_TURNS = 6  # hard stop so a confused model can't loop forever

SYSTEM_PROMPT = """\
You are a Q&A assistant for a video transcript. Answer only from what the \
tools return - never invent dialogue or events.

Tools:
- semantic_search: meaning/theme queries.
- keyword_search: exact names, quotes, specific words.
- fetch_window: transcript around a specific timestamp (for "what happens \
after/before T", or to expand context around a hit you already found).

Call one or more of these as needed, then you MUST finish by calling \
final_answer with a grounded answer and structured citations \
(chunk_id, video_id, start_ms) pointing at the segments you actually used. \
Never answer in plain text - always finish via final_answer. If the tools \
don't support an answer, say so honestly in final_answer with an empty \
citations list rather than guessing.
"""


class AgentResult:
    def __init__(
        self,
        answer: str,
        citations: list[dict],
        trace: list[dict],
        usage: dict | None = None,
    ):
        self.answer = answer
        self.citations = citations
        self.trace = trace  # list of {tool, args, result} for debugging/analytics
        # Summed prompt/completion/total tokens across every model call this
        # turn made (can be >1 - each tool round-trip is its own call). Used
        # for the per-turn analytics event, see PLAN.md feature priority #6.
        self.usage = usage or {}


async def run(
    db: asyncpg.Pool,
    user_message: str,
    *,
    video_id: int | None = None,
    history: list[dict] | None = None,
    emit: EmitFn | None = None,
) -> AgentResult:
    """Run one turn of the agent loop.

    `video_id`, if given, scopes semantic_search/keyword_search to one
    video (the default for a single-video "watch page" chat). `history` is
    prior turns' messages (already OpenAI-shaped) for multi-turn sessions;
    context-management policy (trim/summarize per CLAUDE.md risk #2) is
    intentionally NOT in this bare loop yet - out of scope for the local
    functionality test, see CLAUDE.md open questions. `emit`, if given, is
    awaited with a `{"type": ..., ...}` event right as each real tool call
    starts and finishes, for real-time streaming to the frontend - the
    return value here is still the complete AgentResult either way.
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    trace: list[dict] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for turn in range(MAX_TURNS):
        result = await chat(messages, tools=tools.TOOL_SCHEMAS)
        message = result["message"]
        log.info("turn %d usage=%s", turn, result["usage"])
        for key in usage_totals:
            usage_totals[key] += result["usage"].get(key) or 0

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # Model didn't call a tool at all - treat its text as a
            # non-grounded fallback rather than silently dropping the turn.
            return AgentResult(message.get("content") or "", [], trace, usage_totals)

        # Assistant turn must be appended before its tool results, exactly
        # as the API returned it (needed so tool_call_id round-trips).
        messages.append(message)

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if fn_name == "final_answer":
                trace.append({"tool": fn_name, "args": args, "result": None})
                return AgentResult(
                    args.get("answer", ""),
                    args.get("citations", []),
                    trace,
                    usage_totals,
                )

            if emit:
                await emit(
                    {"type": "tool_call", "call_id": call["id"], "tool": fn_name, "args": args}
                )

            try:
                tool_result = await tools.dispatch(
                    db, fn_name, args, default_video_id=video_id
                )
            except Exception as exc:  # noqa: BLE001 - surface to the model, not a crash
                log.exception("tool %s failed", fn_name)
                tool_result = {"error": str(exc)}

            if emit:
                await emit(
                    {
                        "type": "tool_result",
                        "call_id": call["id"],
                        "tool": fn_name,
                        "result": tool_result,
                    }
                )

            trace.append({"tool": fn_name, "args": args, "result": tool_result})
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(tool_result),
                }
            )

    # Ran out of turns without a final_answer call - degrade honestly.
    return AgentResult(
        "I wasn't able to reach a grounded answer in time - please rephrase "
        "or ask something more specific.",
        [],
        trace,
        usage_totals,
    )
