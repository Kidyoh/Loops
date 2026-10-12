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

## Telegram

```bash
loops telegram-login            # phone + code (+ 2FA); writes an encrypted session
loops sync-telegram             # backfill BACKFILL_DAYS, then incremental; chunks + embeds
```

Synced: DMs and groups you've posted in. Skipped: channels, bots, muted mega-groups.
Override per chat with `TG_CHAT_ALLOWLIST` / `TG_CHAT_DENYLIST` (JSON lists of chat ids).
`TG_MAX_CHATS` caps chats per run to avoid FloodWait on the first backfill.

## Gmail

Create an OAuth client (type "Web application") in Google Cloud, enable the Gmail API,
keep the consent screen in **testing** mode and add your address as a test user. Set the
redirect URI to `GMAIL_REDIRECT_URI`.

```bash
loops gmail-login               # open the URL, approve, paste the redirect URL back
loops sync-gmail                # backfill, then history-based incremental
loops people suggest            # same-name people seen on Gmail and Telegram
loops people merge KEEP OTHER   # merge two people records
```

Promotions, social, spam, trash and drafts are skipped. Quoted replies and signatures are
stripped so only the new text of each email is stored.

## Asking questions

```bash
loops ask "What did Robi say about the Habesha pitch?"
loops ask --mode router "Who am I waiting on?"
```

The agent has four tools: `search_messages` (vector + full-text, merged with reciprocal
rank fusion), `list_commitments`, `get_thread` and `find_person`. Every claim cites
`[source: <message_id>]`, and citations to messages the model never saw are dropped.
If the model answers without looking anything up, or tool calling errors, it falls back
to a fixed router (classify → tool → answer). Set `AGENT_MODE=router` to always use it.

## API

```
POST   /login                    {password} -> session cookie (or send Bearer WEB_PASSWORD)
POST   /ask                      {question, history?} -> SSE: tool, token..., citations, done
GET    /commitments?kind&status&person
PATCH  /commitments/{id}         {status}
GET    /messages/{id}            message + surrounding thread
GET    /sources                  sync status
POST   /sources/gmail/connect    -> {url} to start OAuth
GET    /sources/gmail/callback
DELETE /sources/{id}             wipe a source and everything derived from it
POST   /sync                     trigger an incremental sync
```

Run it with `uvicorn loops.api.main:app` or `docker compose up api`.

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
