"""Unit tests for the retry/error-handling behavior in app/openrouter.py.

No DB, no app import, no real network - respx mocks httpx's transport, so
these run fast and in complete isolation from the rest of the suite.
"""

import httpx
import pytest
import respx

from app import openrouter

COMPLETIONS_URL = f"{openrouter.BASE_URL}/chat/completions"


def _completion_response(content: str = "hi") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


@respx.mock
async def test_chat_retries_once_on_transient_error_then_succeeds():
    route = respx.post(COMPLETIONS_URL).mock(
        side_effect=[
            httpx.RemoteProtocolError("Server disconnected without sending a response"),
            _completion_response("recovered"),
        ]
    )

    result = await openrouter.chat([{"role": "user", "content": "hi"}])

    assert route.call_count == 2
    assert result["message"]["content"] == "recovered"


@respx.mock
async def test_chat_raises_after_exhausting_retries():
    respx.post(COMPLETIONS_URL).mock(
        side_effect=httpx.ConnectError("TLS connect failed")
    )

    with pytest.raises(httpx.ConnectError):
        await openrouter.chat([{"role": "user", "content": "hi"}])


def _sse_body(*data_lines: str) -> bytes:
    return "".join(f"data: {line}\n\n" for line in (*data_lines, "[DONE]")).encode()


@respx.mock
async def test_chat_stream_retries_before_first_chunk_then_succeeds():
    route = respx.post(COMPLETIONS_URL).mock(
        side_effect=[
            httpx.ConnectError("TLS connect failed"),
            httpx.Response(
                200,
                content=_sse_body('{"choices": [{"delta": {"content": "hi"}}]}'),
                headers={"content-type": "text/event-stream"},
            ),
        ]
    )

    chunks = [c async for c in openrouter.chat_stream([{"role": "user", "content": "hi"}])]

    assert route.call_count == 2
    assert chunks == [{"choices": [{"delta": {"content": "hi"}}]}]


class _FakeStreamCtx:
    """A fake `client.stream(...)` async context manager whose body drops
    mid-iteration - respx can't express "raise partway through aiter_lines",
    so this goes one level lower to exercise that exact contract."""

    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line
        raise httpx.RemoteProtocolError("dropped mid-stream")


async def test_chat_stream_does_not_retry_after_first_chunk_yielded(monkeypatch):
    """A transient error is only safe to retry before anything has reached
    the caller - retrying mid-stream would replay/duplicate already-yielded
    content. Once a chunk has been yielded, a later drop must propagate
    immediately, with no second attempt made."""
    call_count = {"n": 0}

    def fake_stream(self, method, url, **kwargs):
        call_count["n"] += 1
        return _FakeStreamCtx(['data: {"choices": [{"delta": {"content": "partial"}}]}'])

    monkeypatch.setattr(httpx.AsyncClient, "stream", fake_stream)

    chunks = []
    with pytest.raises(httpx.RemoteProtocolError):
        async for chunk in openrouter.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

    assert call_count["n"] == 1  # no retry attempted
    assert chunks == [{"choices": [{"delta": {"content": "partial"}}]}]
