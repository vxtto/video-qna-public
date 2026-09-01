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
import re
from typing import Any, AsyncIterator

import asyncpg

from . import queries, tools
from .openrouter import chat, chat_stream

log = logging.getLogger("agent")

MAX_TURNS = 12  # hard stop so a confused model can't loop forever

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
) -> AgentResult:
    """Run one turn of the agent loop, all at once - no partial output until
    it returns. Still used by app/cli.py. For real-time tool-call/answer
    streaming (the /api/chat/stream endpoint), see `run_stream` below.

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
        usage_totals,
    )


_ANSWER_FIELD_RE = re.compile(r'"answer"\s*:\s*"')

# Maps a JSON string escape's second character to the literal it decodes
# to, for _partial_string_field below.
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _partial_string_field(fragment: str, field_re: re.Pattern) -> str | None:
    """Best-effort decode of a string field's value out of a *possibly
    incomplete* JSON object - e.g. `final_answer`'s streamed tool-call
    arguments, mid-flight. Returns everything decoded so far (None if the
    field's opening quote hasn't arrived yet), stopping cleanly at a
    trailing incomplete escape rather than raising.

    Relies on `field_re` matching only once and only field's own key/value
    (fine here: it only ever runs on final_answer's arguments, and
    "answer" is that schema's first property - tools.py - which in
    practice OpenAI-compatible providers stream in schema order). If that
    assumption ever breaks, this just returns stale/no deltas for a turn;
    the non-streamed final answer sent as the "done" event afterwards is
    still correct, so a bad guess here can't corrupt what the user reads.
    """
    m = field_re.search(fragment)
    if not m:
        return None
    body = fragment[m.end() :]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == '"':
            break  # unescaped quote -> string closed
        if c == "\\":
            if i + 1 >= len(body):
                break  # escape byte arrived, its meaning hasn't yet
            nxt = body[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u":
                if i + 6 > len(body):
                    break  # \uXXXX not fully arrived yet
                out.append(chr(int(body[i + 2 : i + 6], 16)))
                i += 6
                continue
            i += 2  # unrecognized escape, skip it
            continue
        out.append(c)
        i += 1
    return "".join(out)


async def run_stream(
    db: asyncpg.Pool,
    user_message: str,
    *,
    video_id: int | None = None,
    history: list[dict] | None = None,
) -> AsyncIterator[dict]:
    """Streaming twin of `run()`, for feature/streaming-chat-responses.

    Same tool loop, same grounding contract, but yields events as they
    happen instead of returning one `AgentResult` at the end:

      {"type": "tool_call", "call_id": ..., "tool": ..., "args": ...}
          - about to dispatch
      {"type": "tool_result", "call_id": ..., "tool": ..., "result": ...}
          - dispatch done, `result` is the same JSON-able value that
            lands in `trace` (or {"error": ...} if the tool raised)
      {"type": "answer_delta", "text": ...}               - answer text grew
      {"type": "done", "answer", "citations", "trace", "usage"}  - final,
          always the last event; authoritative even if deltas drifted.

    `call_id` (the model API's tool_call id) lets a caller correlate a
    `tool_result` back to the `tool_call` that started it - e.g. the
    frontend's live tool-call chips (PLAN.md/feature "tool calls rendered
    in real time"), which render one chip per call_id and flip it from
    pending to done/error as its result arrives.

    The tool-call turns (semantic_search/keyword_search/fetch_window)
    still need their full arguments before they can run, so there's
    nothing to stream mid-turn there - only `tool_call`/`tool_result`
    bracket them, same info the non-streamed trace already carries.
    Token-level streaming is only useful (and only wired up) for the
    final_answer turn's `answer` text, decoded incrementally via
    `_partial_string_field` as its tool-call arguments arrive, plus the
    rare plain-text fallback turn (no tool call at all - see `run()`).
    """
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history or [])
    messages.append({"role": "user", "content": user_message})

    trace: list[dict] = []
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    for turn in range(MAX_TURNS):
        content_parts: list[str] = []
        # index -> accumulated {"id", "name", "arguments"} per OpenAI's
        # streaming tool-call delta shape (each chunk only carries the
        # newly-generated slice of `arguments`).
        calls_acc: dict[int, dict[str, Any]] = {}
        streamed_len = 0  # how much of final_answer's "answer" we've sent

        async for chunk in chat_stream(messages, tools=tools.TOOL_SCHEMAS):
            usage = chunk.get("usage")
            if usage:
                for key in usage_totals:
                    usage_totals[key] += usage.get(key) or 0

            choices = chunk.get("choices") or []
            if not choices:
                continue  # the trailing usage-only chunk has none
            delta = choices[0].get("delta") or {}

            if delta.get("content"):
                content_parts.append(delta["content"])
                yield {"type": "answer_delta", "text": delta["content"]}

            for tc in delta.get("tool_calls") or []:
                acc = calls_acc.setdefault(tc["index"], {"id": None, "name": None, "arguments": ""})
                if tc.get("id"):
                    acc["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    acc["name"] = fn["name"]
                if fn.get("arguments"):
                    acc["arguments"] += fn["arguments"]
                    if acc["name"] == "final_answer":
                        decoded = _partial_string_field(acc["arguments"], _ANSWER_FIELD_RE)
                        if decoded is not None and len(decoded) > streamed_len:
                            yield {"type": "answer_delta", "text": decoded[streamed_len:]}
                            streamed_len = len(decoded)

        log.info("turn %d usage=%s", turn, usage_totals)

        tool_calls = [
            {"id": acc["id"], "type": "function", "function": {"name": acc["name"], "arguments": acc["arguments"]}}
            for _, acc in sorted(calls_acc.items())
        ]

        if not tool_calls:
            # Same non-grounded fallback as run(): model answered in plain
            # text without calling any tool. Already streamed above.
            answer = "".join(content_parts)
            yield {"type": "done", "answer": answer, "citations": [], "trace": trace, "usage": usage_totals}
            return

        messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})

        for call in tool_calls:
            fn_name = call["function"]["name"]
            try:
                args = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}

            if fn_name == "final_answer":
                trace.append({"tool": fn_name, "args": args, "result": None})
                yield {
                    "type": "done",
                    "answer": args.get("answer", ""),
                    "citations": args.get("citations", []),
                    "trace": trace,
                    "usage": usage_totals,
                }
                return

            yield {"type": "tool_call", "call_id": call["id"], "tool": fn_name, "args": args}
            try:
                tool_result = await tools.dispatch(db, fn_name, args, default_video_id=video_id)
            except Exception as exc:  # noqa: BLE001 - surface to the model, not a crash
                log.exception("tool %s failed", fn_name)
                tool_result = {"error": str(exc)}

            trace.append({"tool": fn_name, "args": args, "result": tool_result})
            yield {
                "type": "tool_result",
                "call_id": call["id"],
                "tool": fn_name,
                "result": tool_result,
            }
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(tool_result)}
            )

    yield {
        "type": "done",
        "answer": (
            "I wasn't able to reach a grounded answer in time - please rephrase "
            "or ask something more specific."
        ),
        "citations": [],
        "trace": trace,
        "usage": usage_totals,
    }
