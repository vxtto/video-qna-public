"""Thin OpenRouter client. Both the embedding model (qwen3-embedding-8b)
and the agent LLM (DeepSeek V4 Flash) are OpenAI-compatible endpoints on
the same provider/key, per CLAUDE.md — this project uses one API key for
everything, same as the transcription step in video-processing.
"""

from __future__ import annotations

import os

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
    model: str | None = None,
) -> dict:
    """One DeepSeek chat-completions call. Returns the raw `message` object
    (may contain `tool_calls` or plain `content`)."""
    body: dict = {"model": model or LLM_MODEL, "messages": messages}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
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
