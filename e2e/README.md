# e2e — browser tests

Two Playwright tests covering what needs a real `<video>` element:
clicking a transcript segment seeks the player, and clicking a chat
citation seeks the player. The chat backend is mocked, so no LLM call is
made. They run against a 6-second synthetic clip
(`fixtures/sample_clip.mp4`, generated with ffmpeg's `testsrc`).

```bash
# from the repo root
mkdir -p data/media/raw && cp e2e/fixtures/sample_clip.mp4 data/media/raw/
docker compose up --build -d
docker compose exec -T db psql -U postgres -d video_qna -f - < e2e/seed_fixture.sql

cd e2e && npm ci && npx playwright install --with-deps chromium && npm test
```
