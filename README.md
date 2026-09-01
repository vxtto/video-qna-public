# docker-compose placeholder

Smallest thing that lets a human watch a movie in the browser and confirm
its Whisper transcript is correct, segment by segment, before it's trusted
for retrieval. Not the real RAG backend yet — see [`PLAN.md`](PLAN.md)
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

This mounts `data/media/` (gitignored, not `video-processing/data/` — see
[`video-processing/README.md`](video-processing/README.md) for the
distinction) as a read-only volume. Populate it before first boot:

```bash
mkdir -p data/media/raw data/media/transcripts
cp video-processing/data/raw/*_480p.mp4 data/media/raw/
cp video-processing/data/transcripts/*.json data/media/transcripts/
```

**Working in a worktree under `../video-qna-worktrees/`, and the main
checkout already has `data/media/` populated?** Don't recopy/reprocess —
point at it:

```bash
echo "MEDIA_HOST_DIR=$(cd ../../video-qna && pwd)/data/media" >> .env
```

Then `./scripts/dev-up.sh` below. Same idea for pointing at *any* other
worktree's already-populated copy — see `.env.example` and `PLAN.md`'s
"Running several worktrees in parallel". Only fall back to the
mkdir/cp steps above if no populated copy exists anywhere yet.

## Run it

```bash
cp .env.example .env   # optional, defaults already work
./scripts/dev-up.sh    # self-assigns free host ports, then docker compose up -d --build
```

Only one worktree's stack running on this box? Plain `docker compose up
--build` (foreground) still works fine — `dev-up.sh` only matters once
you're running more than one at a time, see `PLAN.md`.

First boot auto-seeds Postgres from `video-processing/data/transcripts/*.json`
(see [`backend/app/seed.py`](backend/app/seed.py)) if the `videos` table is
empty. Then open the URL `dev-up.sh` printed (or http://localhost:8000 if
you ran compose directly).

To re-seed after editing the manifest in `seed.py`:

```bash
docker compose exec api python -m app.seed
```

## Known placeholder-ness

- Single hardcoded movie (Tears of Steel) in `seed.py`'s `MANIFEST` — add
  entries as the other 3 corpus videos come through the pipeline.
- No auth — fine for local dev only, per `PLAN.md` this needs a
  shared token before anything touches a public VPS.
- No pgvector columns used yet — extension is enabled so the schema doesn't
  need a migration when embeddings show up.
