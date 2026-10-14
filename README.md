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

## Open loops

After chunking and embedding, every thread with new messages goes through Nemotron in
strict JSON mode. It extracts `i_promised`, `they_promised`, `waiting_on` and `postponed`
items, each tied to the message that proves it. Items below `EXTRACT_MIN_CONFIDENCE`
(0.6) are dropped, and so are items whose evidence doesn't fit the kind (an
`i_promised` must come from you). The thread's open items go back into the prompt, so a
later message can mark them done.

A rule that needs no model adds `waiting_on` when the last message in a thread is yours,
asks for something, and has gone `WAITING_ON_DAYS` (3) without a reply. It closes on its
own when someone replies.

```bash
loops process                    # chunk, embed, extract
loops loops --kind waiting_on    # print open loops
```

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

## Telegram bot and daily digest

Create a bot with @BotFather and set `BOT_TOKEN`. Set `OWNER_TELEGRAM_ID` to your
numeric user id. The bot ignores everyone else.

```bash
loops bot        # long-polling bot
loops worker     # sync every SYNC_INTERVAL_MIN (5) min, digest daily at DIGEST_HOUR (8) TIMEZONE
loops digest     # send the digest now
```

Bot commands: `/promises`, `/waiting`, `/postponed`, `/theypromised`, `/done <id>` (or
the ✅ buttons), `/digest`, `/sync`. Any other text goes to the query agent. The answer
streams in by editing the reply, and its citations become numbered links to the source.

The digest lists overdue promises, what's due today, and who you're waiting on. It is
skipped on days with nothing open.

A Postgres advisory lock lets only one sync run at a time across the worker, API and bot.

## Demo data and evals

For recording the demo, use fictional data instead of your real inbox. Point
`DATABASE_URL` at a separate demo database, then:

```bash
loops demo seed                  # ~30 Gmail + Telegram messages over two weeks; chunk, embed, extract
loops eval --out eval-report.md  # 20 questions with expected sources + extraction precision/recall
loops eval --mode router         # same, with the fixed router instead of free tool calling
loops demo reset                 # remove the demo data
```

The demo has promises (made and kept), people you're waiting on, postponed meetings,
and Robi, who shows up on both Telegram and Gmail and gets merged. Dates are relative to
today. `demo seed` refuses to run on a database that already has real sources.

The eval passes an answerable question when the answer cites at least one expected
source, and a negative question when it cites nothing. For extraction it reports
precision and recall of open loops against the expected set. It also flags anything
left open that its own thread already fulfilled.

## Run everything

```bash
docker compose up -d --build     # db, migrate, api, worker, bot
```

`./secrets` is mounted into the containers for the Telegram session. Run
`loops telegram-login` once on the host (or `docker compose run --rm api loops telegram-login`)
before starting the worker.

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
