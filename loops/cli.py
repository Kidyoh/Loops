"""Command line entrypoint: `loops <command>`."""

import argparse
import asyncio
import logging

from loops.core.crypto import generate_key


async def _llm_check() -> None:
    from loops.llm.token_factory import get_llm

    llm = get_llm()
    msg = await llm.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=16)
    print("chat:", msg.get("content"))
    vecs = await llm.embed(["hello world"])
    print("embed dim:", len(vecs[0]))
    await llm.aclose()


async def _sync(kind: str | None) -> None:
    from loops.ingest.pipeline import process_pending

    if kind == "telegram":
        from loops.ingest.telegram import sync_telegram

        print("new telegram messages:", await sync_telegram())
    print(await process_pending())


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="loops")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-key", help="print a new Fernet ENCRYPTION_KEY")
    sub.add_parser("llm-check", help="call chat + embeddings once")
    sub.add_parser("telegram-login", help="log in to Telegram and save the encrypted session")
    sub.add_parser("sync-telegram", help="sync Telegram, then chunk + embed")
    sub.add_parser("process", help="chunk + embed anything pending")
    args = p.parse_args(argv)

    if args.cmd == "gen-key":
        print(generate_key())
    elif args.cmd == "llm-check":
        asyncio.run(_llm_check())
    elif args.cmd == "telegram-login":
        from loops.ingest.telegram import telegram_login

        asyncio.run(telegram_login())
    elif args.cmd == "sync-telegram":
        asyncio.run(_sync("telegram"))
    elif args.cmd == "process":
        asyncio.run(_sync(None))


if __name__ == "__main__":
    main()
