"""Core /api/chat + /api/chat/stream + session/feedback endpoint coverage,
happy paths only - the failure paths live in test_chat_regressions.py."""

from app import agent as agent_module
from tests.conftest import final_answer_response, final_answer_stream_chunks


def test_chat_happy_path_creates_session_and_persists_turn(client, monkeypatch):
    async def _chat(messages, **kwargs):
        return final_answer_response("the answer", citations=[{"chunk_id": 1, "video_id": 1, "start_ms": 5000}])

    monkeypatch.setattr(agent_module, "chat", _chat)

    resp = client.post("/api/chat", json={"message": "who is the pilot?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "the answer"
    assert body["citations"] == [{"chunk_id": 1, "video_id": 1, "start_ms": 5000}]
    assert body["session_id"]
    assert body["message_id"]


def test_chat_continues_existing_session_with_history(client, monkeypatch):
    seen_messages = []
    answers = ["first answer", "second answer"]

    async def _chat(messages, **kwargs):
        seen_messages.append(messages)
        return final_answer_response(answers[len(seen_messages) - 1])

    monkeypatch.setattr(agent_module, "chat", _chat)

    first = client.post("/api/chat", json={"message": "first question"})
    session_id = first.json()["session_id"]

    second = client.post(
        "/api/chat", json={"message": "second question", "session_id": session_id}
    )

    assert second.status_code == 200
    assert second.json()["session_id"] == session_id
    # The second call's history must include the first turn's user+assistant
    # messages - this is the "prior turns replayed into the agent loop" the
    # session feature promises (PLAN.md priority #6).
    second_call_messages = seen_messages[1]
    roles_and_content = [(m["role"], m.get("content")) for m in second_call_messages]
    assert ("user", "first question") in roles_and_content
    assert ("assistant", "first answer") in roles_and_content
    assert ("user", "second question") in roles_and_content


def test_chat_unknown_session_id_returns_404(client):
    resp = client.post(
        "/api/chat",
        json={"message": "hi", "session_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404


def test_chat_unknown_video_slug_returns_404(client):
    resp = client.post("/api/chat", json={"message": "hi", "video_slug": "does-not-exist"})
    assert resp.status_code == 404


def test_chat_stream_happy_path_emits_session_delta_and_final(client, monkeypatch):
    async def _chat_stream(messages, **kwargs):
        async for chunk in final_answer_stream_chunks("streamed answer"):
            yield chunk

    monkeypatch.setattr(agent_module, "chat_stream", _chat_stream)

    resp = client.post("/api/chat/stream", json={"message": "who is the pilot?"})

    assert resp.status_code == 200
    text = resp.text
    assert "event: session" in text
    assert "event: final" in text
    assert '"answer": "streamed answer"' in text


def test_get_session_transcript_returns_persisted_turns(client, monkeypatch):
    async def _chat(messages, **kwargs):
        return final_answer_response("the answer")

    monkeypatch.setattr(agent_module, "chat", _chat)

    chat_resp = client.post("/api/chat", json={"message": "a question"})
    session_id = chat_resp.json()["session_id"]

    resp = client.get(f"/api/sessions/{session_id}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == session_id
    contents = [(m["role"], m["content"]) for m in body["messages"]]
    assert ("user", "a question") in contents
    assert ("assistant", "the answer") in contents


def test_get_session_transcript_unknown_id_returns_404(client):
    resp = client.get("/api/sessions/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404


def test_message_feedback_round_trip(client, monkeypatch):
    async def _chat(messages, **kwargs):
        return final_answer_response("the answer")

    monkeypatch.setattr(agent_module, "chat", _chat)

    chat_resp = client.post("/api/chat", json={"message": "a question"})
    message_id = chat_resp.json()["message_id"]

    resp = client.patch(f"/api/messages/{message_id}/feedback", json={"feedback": "up"})
    assert resp.status_code == 200
    assert resp.json()["feedback"] == "up"


def test_message_feedback_rejects_invalid_value(client):
    resp = client.patch("/api/messages/1/feedback", json={"feedback": "sideways"})
    assert resp.status_code == 422


def test_message_feedback_unknown_message_returns_404(client):
    resp = client.patch("/api/messages/999999/feedback", json={"feedback": "up"})
    assert resp.status_code == 404
