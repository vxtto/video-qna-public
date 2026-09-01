# video-processing

Turns a source movie into what `agent-loop` needs: a 480p mp4 for
streaming, plus per-chunk transcript JSON with timestamps for retrieval
and citations. See the repo's [`../PLAN.md`](../PLAN.md) for the overall
plan, and [`CLAUDE.md`](CLAUDE.md) for gotchas worth reading before you
run this.

## Pipeline (currently ad hoc, not yet scripted)

1. **Download** the source movie (archive.org for the current corpus —
   see `../PLAN.md`'s corpus list).
2. **Transcode** to H.264 480p with `-movflags +faststart`
   (`ffmpeg`) → `data/raw/<slug>_480p.mp4`. Verify faststart actually took
   effect (`moov` atom before `mdat`), not just that the flag was passed —
   `ffprobe` both.
3. **Extract audio** to 16kHz mono wav (Whisper's native format) →
   `data/audio/<slug>.wav`.
4. **Chunk** into 2-minute wav segments → `data/chunks/<slug>_NNN.wav`.
5. **Transcribe** each chunk via OpenRouter's `openai/whisper-large-v3`
   endpoint, with **both** `response_format=verbose_json` and
   `timestamp_granularities[]=segment` set (see `CLAUDE.md` — dropping
   either silently loses timestamps) → `data/transcripts/<slug>_NNN.json`.

Requires `OPENROUTER_API_KEY` in a local `.env` (gitignored, see
[`.env.example`](.env.example)).

## Output → what consumes it

The app does **not** mount this directory directly — `data/` here is the
pipeline's full working area (raw sources at multiple resolutions, audio,
chunks, intermediate transcripts), most of which the app doesn't need.
Publish just the final 480p mp4s + transcript JSON into the repo root's
`data/media/` (see root [`README.md`](../README.md)'s Prerequisites),
which is what `docker-compose.yml` actually mounts and what
`backend/app/seed.py` reads on first boot. Adding a new movie here means
also adding an entry to that file's `MANIFEST` before it shows up in the
app.

## Status

Corpus processing status (which movies are done, costs, known issues per
movie) lives in [`../docs/STATUS.md`](../docs/STATUS.md), not here — check
there before starting on the next movie.
