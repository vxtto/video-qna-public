# docker-compose stack + bare agent loop

Lets a human watch a movie in the browser and confirm its Whisper
transcript is correct, segment by segment, **and** now includes the real
retrieval + agent path (semantic/keyword search, RRF-free hybrid via two
tools, `fetch_window`, and a hand-rolled Hermes-style agent loop against
DeepSeek V4 Flash) — built to be tested locally before it ever touches the
VPS. See [`../PLAN.md`](../PLAN.md) for the overall plan and decisions,
and [`CLAUDE.md`](CLAUDE.md) for local conventions/gotchas.

## Stack

- `db` — Postgres w/ pgvector (`pgvector/pgvector:pg16`), schema applied
  from [`db/migrations/`](db/migrations) in order (numbered `.sql` files,
  no ORM/Alembic — see `CLAUDE.md`'s conventions).
- `api` — FastAPI ([`backend/app/main.py`](backend/app/main.py)):
  - `GET /api/videos`, `GET /api/videos/{slug}` — video + segment metadata
  - `PATCH /api/segments/{id}` — mark a segment correct/incorrect, with an
    optional corrected-text field
  - `POST /api/chat` — runs the agent loop (`app/agent.py`) for one turn;
    body `{"message": str, "video_slug": str | null}`, returns
    `{answer, citations, trace}`
  - `/media/*` — the raw mp4, hand-rolled HTTP Range support (seeking works)
  - `/` — the plain HTML/JS frontend in `backend/static/`, no build step
- All SQL lives in [`backend/app/queries.py`](backend/app/queries.py), one
  function per query, grouped by resource — no ORM.

## Agent loop

- [`app/openrouter.py`](backend/app/openrouter.py) — thin OpenRouter
  client. One `OPENROUTER_API_KEY` for both the embedding model
  (`qwen/qwen3-embedding-8b`) and the LLM (`deepseek/deepseek-v4-flash`),
  same as the transcription step in `video-processing`.
- [`app/tools.py`](backend/app/tools.py) — the three real tools
  (`semantic_search`, `keyword_search`, `fetch_window`) plus a
  `final_answer` tool that structurally enforces citations — the model
  can't finish a turn without emitting `{chunk_id, video_id, start_ms}`.
- [`app/agent.py`](backend/app/agent.py) — the loop itself: model call →
  tool dispatch → append result → repeat until `final_answer`, capped at
  `MAX_TURNS`. No skills system, no sub-agents, no scheduling — intentionally
  bare (see `../PLAN.md`'s Hermes decision).
- [`app/cli.py`](backend/app/cli.py) — local REPL for driving the loop
  without the frontend, e.g. `python -m app.cli tears-of-steel`.
- No context-management policy (trim/summarize) yet — out of scope for
  this bare-loop pass, tracked as feature priority #5 in `../PLAN.md`.

## Prerequisites

This mounts movie files + transcript JSON from the **`video-processing`**
worktree as a read-only volume (`../video-processing/data`), so that
worktree must exist as a sibling directory:

```
video-qna-worktrees/
├── agent-loop/        ← you are here
└── video-processing/  ← data/raw/*.mp4, data/transcripts/*.json
```

## Run it

```bash
cp .env.example .env   # then fill in OPENROUTER_API_KEY
docker compose up --build
```

First boot auto-seeds Postgres from `video-processing/data/transcripts/*.json`
(see [`backend/app/seed.py`](backend/app/seed.py)) if the `videos` table is
empty. Then, once, backfill embeddings for the seeded segments:

```bash
docker compose exec api python -m app.embed_segments
```

Then either open http://localhost:8000, or hit the agent directly:

```bash
curl -s localhost:8000/api/chat -H 'content-type: application/json' \
  -d '{"message": "Who is Tom and what is he freaked out about?", "video_slug": "tears-of-steel"}'
```

Verified locally end-to-end (2026-09-01): `keyword_search` +
`semantic_search` + `fetch_window` all correctly dispatched across a
multi-turn tool-call loop, closed with a grounded `final_answer` and
correct `{chunk_id, start_ms}` citations; a name not present in the
transcript (typo'd query) correctly came back with an honest "not found"
instead of a hallucinated answer.

To re-seed after editing the manifest in `seed.py`:

```bash
docker compose exec api python -m app.seed
```

## Known placeholder-ness

- Single hardcoded movie (Tears of Steel) in `seed.py`'s `MANIFEST` — add
  entries as the other 3 corpus videos come through the pipeline.
- No auth — fine for local dev only, per `../PLAN.md` this needs a
  shared token before anything touches a public VPS.
- `/api/chat` is one-shot per call, no server-side session storage yet
  (`../PLAN.md` feature priority #6 — sessions/messages/events tables
  still an open item).
- No HNSW/ivfflat index on `embedding` — exact scan, per `../PLAN.md`
  (and pgvector's 2000-dim index cap wouldn't fit our 4096-dim vectors
  anyway).
