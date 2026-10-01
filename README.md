# video-qna

Ask a question about a film in plain English and get an answer grounded in
its transcript, with `[MM:SS]` citations that seek the video player to the
moment being quoted.

![Asking a question and jumping to the cited moment](docs/demo.gif)

A small RAG project: FastAPI, Postgres + pgvector, a hand-rolled agent loop,
and a build-free HTML/JS frontend.

## How it works

- **Ingest** — films are transcribed with Whisper into timestamped segments,
  stored in Postgres with an embedding and a full-text column
  ([`db/migrations/`](db/migrations/), [`docs/SCHEMA.md`](docs/SCHEMA.md)).
- **Agent** — [`backend/app/agent.py`](backend/app/agent.py) is the whole
  agent: model call → tool dispatch → repeat, no framework. It must finish
  by calling `final_answer` with structured citations, so it cannot answer
  without pointing at segments it retrieved.
- **Tools** — [`backend/app/tools.py`](backend/app/tools.py):
  `semantic_search` (pgvector), `keyword_search` (Postgres full-text),
  `fetch_window` (transcript around a timestamp) and `search_chapters`
  (LLM-generated chapter index).
- **API** — [`backend/app/main.py`](backend/app/main.py): chat over SSE with
  tool calls streamed live, server-side chat sessions, and Range-request
  video streaming.

Models run through [OpenRouter](https://openrouter.ai): DeepSeek for the
agent, `qwen3-embedding-8b` for embeddings.

## Run it

Transcripts for two Blender open movies are included; the videos are not.
Put 480p H.264 encodes at `data/media/raw/tears_of_steel_480p.mp4` and
`data/media/raw/cosmos_laundromat_480p.mp4`, then:

```bash
cp .env.example .env        # set OPENROUTER_API_KEY
docker compose up --build -d
docker compose exec api python -m app.embed_segments      # one-off
docker compose exec api python -m app.generate_chapters   # one-off
```

Open http://localhost:8000.

## Tests

```bash
cd backend && pip install -r requirements-dev.txt
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/video_qna pytest
```

Unit and integration tests run against a real Postgres with the LLM mocked;
two Playwright tests in [`e2e/`](e2e/) cover click-to-seek. CI runs lint,
migrations, a compose smoke test and both suites.

## Limitations

- No retrieval eval harness; answer quality was checked by hand.
- No auth of its own — it expects an authenticating reverse proxy
  ([`backend/app/auth.py`](backend/app/auth.py)).
- Chat history is replayed in full each turn, with no trimming.

## Licence

Code: MIT. Transcripts derive from *Tears of Steel* and *Cosmos Laundromat*,
© Blender Foundation, used under their Creative Commons licences.
