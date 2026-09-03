"""Thin OpenRouter client. Both the embedding model (qwen3-embedding-8b)
and the agent LLM (DeepSeek V4 Flash) are OpenAI-compatible endpoints on
the same provider/key, per CLAUDE.md — this project uses one API key for
everything, same as the transcription step in video-processing.
"""

from __future__ import annotations

import json
import os
from typing import AsyncIterator

import httpx

BASE_URL = "https://openrouter.ai/api/v1"

API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "qwen/qwen3-embedding-8b")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek/deepseek-v4-flash")

_HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
    # Optional but polite per OpenRouter's docs — identifies the app in
    # their dashboard, no functional effect.
    "X-Title": "video-qna (agent loop, local test)",
}


async def embed(texts: list[str], *, model: str | None = None) -> list[list[float]]:
    """Returns one embedding vector per input text, same order."""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{BASE_URL}/embeddings",
            headers=_HEADERS,
            json={"model": model or EMBEDDING_MODEL, "input": texts},
        )
    resp.raise_for_status()
    data = resp.json()["data"]
    # API doesn't guarantee order without an explicit index; sort defensively.
    data.sort(key=lambda d: d.get("index", 0))
    return [d["embedding"] for d in data]


async def chat(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    response_format: dict | None = None,
    model: str | None = None,
) -> dict:
    """One DeepSeek chat-completions call. Returns the raw `message` object
    (may contain `tool_calls` or plain `content`).

    `response_format` is DeepSeek's structured-outputs param (JSON schema,
    per CLAUDE.md's core stack decisions) — used by app/generate_chapters.py
    for a plain (no tool-calling) call that must come back as strict JSON,
    mutually exclusive with `tools` in practice (nothing here needs both)."""
    body: dict = {"model": model or LLM_MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    if response_format:
        body["response_format"] = response_format
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{BASE_URL}/chat/completions", headers=_HEADERS, json=body
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenRouter {resp.status_code}: {resp.text[:2000]}")
    payload = resp.json()
    choice = payload["choices"][0]
    usage = payload.get("usage", {})
    return {"message": choice["message"], "usage": usage}


async def chat_stream(
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    model: str | None = None,
) -> AsyncIterator[dict]:
    """Streamed variant of `chat()`, for feature/streaming-chat-responses.
    Yields the raw OpenAI-compatible SSE chunk objects
    (`{"choices": [{"delta": {...}, "finish_reason": ...}], ...}`) as they
    arrive, plus a final usage-only chunk (`stream_options.include_usage`).
    Caller (agent.run_stream) owns accumulating deltas into a message -
    this stays a thin transport layer, same spirit as `chat()` above.
    """
    body: dict = {
        "model": model or LLM_MODEL,
        "messages": messages,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    async with httpx.AsyncClient(timeout=120) as client:
        async with client.stream(
            "POST", f"{BASE_URL}/chat/completions", headers=_HEADERS, json=body
        ) as resp:
            if resp.status_code >= 400:
                body_bytes = await resp.aread()
                raise RuntimeError(
                    f"OpenRouter {resp.status_code}: {body_bytes[:2000]!r}"
                )
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue  # blank lines / OpenRouter ": keep-alive" comments
                data = line[len("data:") :].strip()
                if data == "[DONE]":
                    break
                yield json.loads(data)
