# Loops: Devpost write-up (draft)

> Fill in the `TODO` numbers after running `loops eval` on the demo database with the
> real models.

## Tagline

Never drop a promise again: private search and Q&A over your Gmail and Telegram,
running only on open models.

## Inspiration

Promises and follow-ups die in the inbox. "I'll send it Friday" goes out on Telegram, the
reminder comes by email, and a week later nobody remembers who owes what. Assistants that
could help want your messages sent to a closed model. For your whole personal
correspondence, that is a lot to hand over.

## What it does

Loops reads your Gmail and Telegram (DMs and the groups you actually take part in) and
answers questions about them, citing the original message for every claim:

- **"What did I promise people this month?"**
- **"Who am I waiting on?"**
- **"What have I postponed?"**
- **"What did Robi say about the Habesha pitch?"**

It also keeps a live list of **open loops**: promises you made, promises made to you,
people you're waiting on, and things that got postponed. Each one links to the message
that proves it. Loops closes an item when a later message shows it was handled. Every
morning at 8:00 a Telegram bot sends a digest of overdue promises, what's due today, and
who hasn't replied.

## How we built it

- **Ingest:** Telethon (MTProto) logs in as your own Telegram account. The Gmail API uses
  OAuth in testing mode. There is a 90-day backfill, then incremental sync every 5
  minutes (Gmail `history.list`, per-chat Telegram cursors).
- **Normalize and chunk:** quoted replies and signatures are stripped from email. People
  are resolved across sources. Chat messages are grouped into overlapping windows.
- **Retrieval:** Postgres 16 + pgvector. Hybrid search combines HNSW vector similarity
  with Postgres full-text search, merged with reciprocal rank fusion.
- **Open-loop extraction:** NVIDIA Nemotron on Nebius Token Factory runs per thread on
  new messages only, in strict JSON. Each item must point at its evidence message and
  match its kind (an "I promised" must come from you), and anything below 0.6 confidence
  is dropped. A second check needs no model at all: "my message asked for something and
  three days passed without a reply".
- **Query agent:** Nemotron with tool calling (`search_messages`, `list_commitments`,
  `get_thread`, `find_person`). If the model answers without looking anything up, a fixed
  router takes over. Citations to messages the model never saw are thrown away.
- **Interfaces:** FastAPI with streamed answers over SSE, and a Telegram bot that only
  answers its owner.
- **Deploy:** one Nebius AI Cloud VM with Docker Compose and Caddy for HTTPS.

## Privacy

- Every model call goes to open-weight models (Nemotron for chat and extraction, an open
  embedding model) on Nebius. No closed-model APIs anywhere in the pipeline.
- Data stays in your own Nebius tenant: one VM, one Postgres.
- The Gmail refresh token and the Telegram session are encrypted at rest.
- The bot ignores everyone but you. One call wipes a source and everything derived from it.

## Evaluation

The repo includes a fictional two-week dataset and a 20-question eval with expected
sources (including questions that should find nothing), plus a scored check of open-loop
extraction.

| Metric | Tool calling | Fixed router |
|---|---|---|
| Questions passed (of 20) | TODO | TODO |
| Mean source recall | TODO | TODO |
| Mean citation precision | TODO | TODO |
| Open-loop precision / recall | TODO | TODO |

## Challenges

- Telegram FloodWait on the first backfill: we throttle, cap the number of chats, and
  sleep on rate limits.
- Large embeddings: HNSW in pgvector caps at 2000 dimensions, so bigger models are
  indexed through a `halfvec` cast.
- Extraction noise: precision matters more than recall for open loops, so we validate
  evidence and direction for every item and prefer missing an item to inventing one.

## What's next

- A small web view (Ask, Open loops board, Sources)
- More sources (WhatsApp exports, Slack)
- Multi-user accounts, with each user's data in their own tenant

## Built with

Python, FastAPI, PostgreSQL, pgvector, NVIDIA Nemotron, Nebius Token Factory, Nebius AI
Cloud, Telethon, python-telegram-bot, Gmail API, Docker, Caddy

---

# Demo video script (2–3 min)

Record against the demo database (`loops demo seed`), not your real accounts.

1. **Problem (20s):** promises and follow-ups die between Gmail and Telegram. Show the two
   apps side by side with Robi's "deck" message on Telegram and Hanna's reminder email.
2. **Ask the bot (40s):** on your phone, ask *"What did I promise people this month?"*.
   The answer streams in with numbered sources: sender, date and thread for each one.
3. **Cross-source (30s):** ask *"Who am I waiting on?"*. The answer mixes Dawit's logo files
   (Telegram) and the venue booking email (Gmail).
4. **Open loops (30s):** `/promises`, tap ✅ on one item. Then `/digest` to show the
   morning digest.
5. **Privacy (20s):** one slide: Nemotron on Nebius Token Factory, open weights, your own
   tenant, encrypted credentials, no closed-model APIs.
