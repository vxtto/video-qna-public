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

import asyncpg

from . import queries, tools
from .openrouter import chat

log = logging.getLogger("agent")

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
    def __init__(self, answer: str, citations: list[dict], trace: list[dict]):
        self.answer = answer
        self.citations = citations
        self.trace = trace  # list of {tool, args, result} for debugging/analytics


async def run(
    db: asyncpg.Pool,
    user_message: str,
    *,
    video_id: int | None = None,
    history: list[dict] | None = None,
) -> AgentResult:
    """Run one turn of the agent loop.

    `video_id`, if given, scopes semantic_search/keyword_search to one
    video (the default for a single-video "watch page" chat). `history` is
    prior turns' messages (already OpenAI-shaped) for multi-turn sessions;
    context-management policy (trim/summarize per CLAUDE.md risk #2) is
    intentionally NOT in this bare loop yet - out of scope for the local
    functionality test, see CLAUDE.md open questions.
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    trace: list[dict] = []

    for turn in range(MAX_TURNS):
        result = await chat(messages, tools=tools.TOOL_SCHEMAS)
        message = result["message"]
        log.info("turn %d usage=%s", turn, result["usage"])

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            # Model didn't call a tool at all - treat its text as a
            # non-grounded fallback rather than silently dropping the turn.
            return AgentResult(message.get("content") or "", [], trace)

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
                    args.get("answer", ""), args.get("citations", []), trace
                )

            try:
                tool_result = await tools.dispatch(
                    db, fn_name, args, default_video_id=video_id
                )
            except Exception as exc:  # noqa: BLE001 - surface to the model, not a crash
                log.exception("tool %s failed", fn_name)
                tool_result = {"error": str(exc)}

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
    )
