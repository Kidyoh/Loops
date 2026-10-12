# Loops

Private search and Q&A over your Gmail and Telegram, running entirely on open models
(NVIDIA Nemotron) on Nebius Token Factory. It answers questions like *"What did I promise
people this month?"*, *"Who am I waiting on?"* and *"What have I postponed?"*, citing the
source message for every claim.

## Layout

```
loops/
  api/       FastAPI app
  agent/     query agent, tools, prompts
  ingest/    gmail, telegram, normalize, chunk, embed
  extract/   commitment extraction + prompts
  llm/       Token Factory client (chat + embeddings)
  db/        models, alembic migrations
  bot/       Telegram bot + daily digest
  worker/    scheduler entrypoint
  core/      config, encryption
```

## Setup

```bash
cp .env.example .env            # fill in keys
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
loops gen-key                   # paste into ENCRYPTION_KEY
docker compose up -d db
alembic upgrade head
loops llm-check                 # one chat + one embedding call
```

`EMBED_DIM` must match the embedding model output. Above 2000 dimensions the HNSW
index is built on a `halfvec` cast (needs pgvector >= 0.7, which the compose image has).

## Tests

```bash
pytest                                              # unit tests
NEBIUS_API_KEY=... pytest -m live                   # real chat + embed calls
TEST_DATABASE_URL=postgresql+asyncpg://... pytest   # also runs DB tests (needs pgvector)
```

## Privacy

All inference goes to open-weight models on Nebius. No closed-model APIs are called.
Gmail refresh tokens and the Telethon session are encrypted at rest with `ENCRYPTION_KEY`.
Never commit `secrets/` or any `*.session` file.
