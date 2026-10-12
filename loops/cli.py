"""Command line entrypoint: `loops <command>`."""

import argparse
import asyncio
import logging

from loops.core.crypto import generate_key


SYNC_COMMANDS = {"sync": "all", "sync-telegram": "telegram", "sync-gmail": "gmail"}


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

    if kind in ("telegram", "all"):
        from loops.ingest.telegram import sync_telegram

        print("new telegram messages:", await sync_telegram())
    if kind in ("gmail", "all"):
        from loops.ingest.gmail import sync_gmail

        print("new gmail messages:", await sync_gmail())
    print(await process_pending())


async def _gmail_login() -> None:
    import secrets
    from urllib.parse import parse_qs, urlparse

    from loops.ingest.gmail import authorization_url, exchange_code

    state = secrets.token_urlsafe(16)
    print("Open this URL, approve, then paste the full URL you were redirected to:\n")
    print(authorization_url(state), "\n")
    redirected = input("Redirect URL: ").strip()
    query = parse_qs(urlparse(redirected).query)
    if query.get("state", [None])[0] != state:
        raise SystemExit("state mismatch; start again")
    source = await exchange_code(query["code"][0])
    print(f"Connected Gmail account {source.account}")


async def _people(action: str, ids: list[int], apply: bool) -> None:
    from loops.db.session import session_scope
    from loops.ingest.normalize import merge_people, suggest_merges

    async with session_scope() as session:
        if action == "merge":
            if len(ids) != 2:
                raise SystemExit("usage: loops people merge KEEP_ID OTHER_ID")
            kept = await merge_people(session, ids[0], ids[1])
            print(f"merged into {kept.id} {kept.display_name}")
            return
        pairs = await suggest_merges(session)
        for mail, tg in pairs:
            print(f"{mail.id} {mail.display_name} <{', '.join(mail.emails)}>  ==  {tg.id} {tg.display_name} (telegram)")
            if apply:
                await merge_people(session, mail.id, tg.id)
        if not pairs:
            print("no suggestions")


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="loops")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("gen-key", help="print a new Fernet ENCRYPTION_KEY")
    sub.add_parser("llm-check", help="call chat + embeddings once")
    sub.add_parser("telegram-login", help="log in to Telegram and save the encrypted session")
    sub.add_parser("sync-telegram", help="sync Telegram, then chunk + embed")
    sub.add_parser("gmail-login", help="connect Gmail via OAuth (paste the redirect URL)")
    sub.add_parser("sync-gmail", help="sync Gmail, then chunk + embed")
    sub.add_parser("sync", help="sync every source, then chunk + embed")
    sub.add_parser("process", help="chunk + embed anything pending")
    people = sub.add_parser("people", help="review cross-source people merges")
    people.add_argument("action", choices=["suggest", "merge"])
    people.add_argument("ids", nargs="*", type=int, help="merge: KEEP_ID OTHER_ID")
    people.add_argument("--apply", action="store_true", help="suggest: merge every suggested pair")
    args = p.parse_args(argv)

    if args.cmd == "gen-key":
        print(generate_key())
    elif args.cmd == "llm-check":
        asyncio.run(_llm_check())
    elif args.cmd == "telegram-login":
        from loops.ingest.telegram import telegram_login

        asyncio.run(telegram_login())
    elif args.cmd == "gmail-login":
        asyncio.run(_gmail_login())
    elif args.cmd in SYNC_COMMANDS:
        asyncio.run(_sync(SYNC_COMMANDS[args.cmd]))
    elif args.cmd == "process":
        asyncio.run(_sync(None))
    elif args.cmd == "people":
        asyncio.run(_people(args.action, args.ids, args.apply))


if __name__ == "__main__":
    main()
