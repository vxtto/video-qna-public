# e2e — browser tests

Two Playwright tests, deliberately kept to exactly that many (see PLAN.md /
repo history for why): a real browser is the only way to test the plain-JS,
no-build-step frontend at all, but it's also the slowest and most
flaky-prone layer in the suite, so it only covers what genuinely needs a
live `<video>` element and real DOM events - clicking a transcript segment
seeks the player, and clicking a chat citation seeks the player. Everything
about answer/retrieval *quality* is out of scope here - that's the separate
eval harness (PLAN.md feature priority #4).

Real movie files (`data/media/`) are gitignored and never present outside a
dev/prod box - see CLAUDE.md. These tests instead use a tiny synthetic
fixture clip committed via git-lfs: `e2e/fixtures/sample_clip.mp4` (6s,
320x240, generated with ffmpeg's `testsrc`/`sine`, no licensing question).

## Running locally

```bash
# from the repo root
mkdir -p data/media/raw
cp e2e/fixtures/sample_clip.mp4 data/media/raw/
docker compose up --build -d db api
# wait for the api healthcheck / http://127.0.0.1:8000/api/videos to answer
docker compose exec -T db psql -U postgres -d video_qna -f - < e2e/seed_fixture.sql

cd e2e
npm ci
npx playwright install --with-deps chromium
npm test
```

`E2E_BASE_URL` overrides the default `http://127.0.0.1:8000` if the stack
is exposed elsewhere (see `playwright.config.ts`).

## CI

Runs as its own non-blocking job (not a required check) - see
`.github/workflows/pr-validation.yml`'s `e2e` job. Non-blocking because a
live-browser layer is inherently the flakiest thing in the suite; promote
it to required once it's proven stable over time.
