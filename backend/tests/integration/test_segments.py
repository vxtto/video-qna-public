"""GET /api/videos, GET /api/videos/{slug}, PATCH /api/segments/{id} -
the video/transcript-review endpoints."""


async def test_list_videos_includes_counts(client, seeded_video):
    resp = client.get("/api/videos")

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["slug"] == "demo"
    assert body[0]["segment_count"] == 2
    assert body[0]["reviewed_count"] == 0


async def test_get_video_by_slug_includes_segments(client, seeded_video):
    resp = client.get("/api/videos/demo")

    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Demo Video"
    assert [s["text"] for s in body["segments"]] == ["hello there", "general kenobi"]


def test_get_video_unknown_slug_returns_404(client):
    resp = client.get("/api/videos/does-not-exist")
    assert resp.status_code == 404


async def test_review_segment_updates_status_and_corrected_text(client, seeded_video):
    segment_id = client.get("/api/videos/demo").json()["segments"][0]["id"]

    resp = client.patch(
        f"/api/segments/{segment_id}",
        json={"review_status": "incorrect", "corrected_text": "hello there!"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "incorrect"
    assert body["corrected_text"] == "hello there!"

    # Reflected back on the video/segments list too, not just the PATCH echo.
    reviewed_count = client.get("/api/videos").json()[0]["reviewed_count"]
    assert reviewed_count == 1


def test_review_segment_rejects_invalid_status(client):
    resp = client.patch("/api/segments/1", json={"review_status": "sideways"})
    assert resp.status_code == 422


def test_review_segment_unknown_id_returns_404(client):
    resp = client.patch("/api/segments/999999", json={"review_status": "correct"})
    assert resp.status_code == 404
