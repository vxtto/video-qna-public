"""Regression tests for real, live-observed bugs (see
memory/no-test-suite-openrouter-failure-handling) - not hypothetical edge
cases. Both were reproduced against a running deployment before any test
existed:

1. `/api/chat` had zero error handling around `agent.run` - an OpenRouter
   transport failure propagated as a bare unhandled 500 with a full stack
   trace.
2. `/api/chat/stream` caught the exception but dropped the turn entirely -
   `record_turn` only ran after the try block succeeded, so a mid-stream
   drop meant the user's message was never persisted.

Both are mocked at `app.agent.chat`/`chat_stream` - the exact seam the
memory recommends - rather than going through respx/openrouter, since the
point here is main.py's handling of a failure, not openrouter.py's own
retry behavior (that's tests/unit/test_openrouter.py's job).
"""

import asyncpg
import httpx

from app import agent as agent_module
from app.db import DATABASE_URL


async def _the_only_session_id() -> str:
    """Each test starts from an empty DB (conftest's autouse _clean_db) and
    every scenario here creates exactly one session - so "the one row" is
    an unambiguous way to fetch it back without threading session_id
    through the (deliberately opaque) error response body."""
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        return str(await conn.fetchval("SELECT id FROM sessions"))
    finally:
        await conn.close()


async def _messages_for_session(session_id: str) -> list[dict]:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            "SELECT role, content FROM messages WHERE session_id = $1::uuid ORDER BY id",
            session_id,
        )
        return [dict(r) for r in rows]
    finally:
        await conn.close()


def test_chat_returns_clean_error_on_transport_failure(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    monkeypatch.setattr(agent_module, "chat", _raise)

    resp = client.post("/api/chat", json={"message": "what happens in this scene?"})

    assert resp.status_code == 502
    body = resp.json()
    assert "detail" in body
    # A raw 500 traceback would contain the exception's module path and
    # "Traceback" - neither should ever reach the client.
    assert "Traceback" not in body["detail"]
    assert "RemoteProtocolError" not in body["detail"]


async def test_chat_persists_user_message_when_agent_run_fails(client, monkeypatch):
    async def _raise(*args, **kwargs):
        raise httpx.RemoteProtocolError("Server disconnected without sending a response")

    monkeypatch.setattr(agent_module, "chat", _raise)

    resp = client.post("/api/chat", json={"message": "what happens in this scene?"})
    assert resp.status_code == 502

    session_id = await _the_only_session_id()
    messages = await _messages_for_session(session_id)
    assert messages == [{"role": "user", "content": "what happens in this scene?"}]


async def test_chat_stream_persists_user_message_on_transport_failure(client, monkeypatch):
    async def _raise_stream(*args, **kwargs):
        raise httpx.ConnectError("TLS connect failed")
        yield  # pragma: no cover - unreachable, keeps this a valid async generator

    monkeypatch.setattr(agent_module, "chat_stream", _raise_stream)

    resp = client.post("/api/chat/stream", json={"message": "what happens in this scene?"})

    assert resp.status_code == 200  # SSE: failure is an in-band `error` event, not an HTTP error
    assert "event: error" in resp.text

    session_id = await _the_only_session_id()
    messages = await _messages_for_session(session_id)
    assert messages == [{"role": "user", "content": "what happens in this scene?"}]
