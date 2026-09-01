# docker-compose placeholder

Smallest thing that lets a human watch a movie in the browser and confirm
its Whisper transcript is correct, segment by segment, before it's trusted
for retrieval. Not the real RAG backend yet — see the repo's `CLAUDE.md`
for the overall plan.

## Stack

- `db` — Postgres w/ pgvector (`pgvector/pgvector:pg16`), schema in
  [`db/init.sql`](db/init.sql).
- `api` — FastAPI ([`backend/app/main.py`](backend/app/main.py)), serves:
  - `GET /api/videos`, `GET /api/videos/{slug}` — video + segment metadata
  - `PATCH /api/segments/{id}` — mark a segment correct/incorrect, with an
    optional corrected-text field
  - `/media/*` — the raw mp4, via `StaticFiles` (supports HTTP Range, so
    `<video>` seeking works)
  - `/` — the plain HTML/JS frontend in `backend/static/`, no build step

## Prerequisites

This mounts movie files + transcript JSON from the **`video-processing`**
worktree as a read-only volume (`../video-processing/data`), so that
worktree must exist as a sibling directory:

```
video-qna-worktrees/
├── docker-compose/   ← you are here
└── video-processing/ ← data/raw/*.mp4, data/transcripts/*.json
```

## Run it

```bash
cp .env.example .env   # optional, defaults already work
docker compose up --build
```

First boot auto-seeds Postgres from `video-processing/data/transcripts/*.json`
(see [`backend/app/seed.py`](backend/app/seed.py)) if the `videos` table is
empty. Then open http://localhost:8000.

To re-seed after editing the manifest in `seed.py`:

```bash
docker compose exec api python -m app.seed
```

## Known placeholder-ness

- Single hardcoded movie (Tears of Steel) in `seed.py`'s `MANIFEST` — add
  entries as the other 3 corpus videos come through the pipeline.
- No auth — fine for local dev only, per CLAUDE.md risk #8 this needs a
  shared token before anything touches a public VPS.
- No pgvector columns used yet — extension is enabled so the schema doesn't
  need a migration when embeddings show up.
