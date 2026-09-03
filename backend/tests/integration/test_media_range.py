"""GET /media/{filename} - hand-rolled HTTP Range support (main.py notes
Starlette's StaticFiles/FileResponse doesn't implement Range at all, so
this is bespoke code with zero framework safety net; seeking in the video
player depends on every one of these paths being right)."""

import shutil
from pathlib import Path

import pytest

from app import main as main_module

FIXTURE_CLIP = Path(__file__).resolve().parents[3] / "e2e" / "fixtures" / "sample_clip.mp4"


@pytest.fixture
def media_file(tmp_path, monkeypatch):
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / "sample_clip.mp4"
    shutil.copy(FIXTURE_CLIP, dest)
    monkeypatch.setattr(main_module, "MEDIA_ROOT", tmp_path)
    return dest


def test_full_response_without_range_header(client, media_file):
    resp = client.get("/media/sample_clip.mp4")

    assert resp.status_code == 200
    assert resp.headers["accept-ranges"] == "bytes"
    assert int(resp.headers["content-length"]) == media_file.stat().st_size
    assert resp.content == media_file.read_bytes()


def test_partial_range_response(client, media_file):
    resp = client.get("/media/sample_clip.mp4", headers={"range": "bytes=0-99"})

    size = media_file.stat().st_size
    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-99/{size}"
    assert resp.headers["content-length"] == "100"
    assert resp.content == media_file.read_bytes()[:100]


def test_open_ended_range_response(client, media_file):
    size = media_file.stat().st_size
    resp = client.get("/media/sample_clip.mp4", headers={"range": f"bytes={size - 50}-"})

    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes {size - 50}-{size - 1}/{size}"
    assert resp.content == media_file.read_bytes()[-50:]


def test_range_end_beyond_file_size_is_clamped(client, media_file):
    size = media_file.stat().st_size
    resp = client.get("/media/sample_clip.mp4", headers={"range": f"bytes=0-{size * 10}"})

    assert resp.status_code == 206
    assert resp.headers["content-range"] == f"bytes 0-{size - 1}/{size}"
    assert resp.content == media_file.read_bytes()


def test_malformed_range_returns_416(client, media_file):
    resp = client.get("/media/sample_clip.mp4", headers={"range": "bytes=500-100"})

    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{media_file.stat().st_size}"


def test_non_bytes_unit_returns_416(client, media_file):
    resp = client.get("/media/sample_clip.mp4", headers={"range": "frames=0-10"})
    assert resp.status_code == 416


def test_missing_file_returns_404(client, media_file):
    resp = client.get("/media/does-not-exist.mp4")
    assert resp.status_code == 404
