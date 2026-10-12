"""Telegram ingest through your own user account (Telethon / MTProto).

Scope: DMs plus groups you've sent a message in. Channels are skipped, and so are muted
mega-groups. TG_CHAT_ALLOWLIST / TG_CHAT_DENYLIST override those rules per chat id.

The Telethon session is effectively your account password. It is stored as a
Fernet-encrypted StringSession at TG_SESSION_PATH and must never be committed.
"""

import asyncio
import getpass
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from loops.core.config import Settings, get_settings
from loops.core.crypto import decrypt, encrypt
from loops.db.models import Source
from loops.db.session import session_scope
from loops.ingest.normalize import get_or_create_source, resolve_person, store_messages
from loops.ingest.state import get_state, set_state
from loops.ingest.types import IncomingMessage, SenderRef

log = logging.getLogger(__name__)

SKIP_CURSOR = "skip"
SKIP_RECHECK = timedelta(days=1)
FLOOD_RETRIES = 3


# --- session handling -----------------------------------------------------------------


def _load_session_string(s: Settings) -> str:
    path = Path(s.tg_session_path)
    if not path.exists():
        raise RuntimeError(f"No Telegram session at {path}. Run: loops telegram-login")
    return decrypt(path.read_bytes()).decode()


def _save_session_string(s: Settings, value: str) -> None:
    path = Path(s.tg_session_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encrypt(value.encode()))
    path.chmod(0o600)


@asynccontextmanager
async def open_client(s: Settings | None = None) -> AsyncIterator[Any]:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    s = s or get_settings()
    client = TelegramClient(StringSession(_load_session_string(s)), s.tg_api_id, s.tg_api_hash)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized. Run: loops telegram-login")
        yield client
    finally:
        await client.disconnect()


async def telegram_login() -> None:
    """Interactive login; writes the encrypted session file."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    s = get_settings()
    client = TelegramClient(StringSession(), s.tg_api_id, s.tg_api_hash)
    await client.start(
        phone=lambda: input("Phone number (international format): "),
        code_callback=lambda: input("Login code: "),
        password=lambda: getpass.getpass("2FA password (if any): "),
    )
    _save_session_string(s, client.session.save())
    me = await client.get_me()
    await client.disconnect()
    print(f"Logged in as {display_name(me)} ({me.id}). Session saved to {s.tg_session_path}")


# --- conversion -----------------------------------------------------------------------


def display_name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if title:
        return title
    name = " ".join(p for p in (getattr(entity, "first_name", None), getattr(entity, "last_name", None)) if p)
    return name or getattr(entity, "username", None) or str(getattr(entity, "id", "unknown"))


def message_url(entity: Any, msg_id: int) -> str | None:
    username = getattr(entity, "username", None)
    is_user = hasattr(entity, "first_name")
    if username and not is_user:
        return f"https://t.me/{username}/{msg_id}"
    if getattr(entity, "megagroup", False) or getattr(entity, "broadcast", False):
        return f"https://t.me/c/{entity.id}/{msg_id}"
    return None  # DMs and basic groups have no public deep link


def to_incoming(msg: Any, chat_id: int, entity: Any) -> IncomingMessage | None:
    text = (getattr(msg, "message", None) or "").strip()
    if not text:
        return None  # media without caption, service messages
    sender = getattr(msg, "sender", None)
    sender_id = getattr(msg, "sender_id", None)
    return IncomingMessage(
        thread_external_id=str(chat_id),
        thread_title=display_name(entity),
        external_id=str(msg.id),
        sender=SenderRef(name=display_name(sender) if sender else "", telegram_id=sender_id),
        is_from_me=bool(getattr(msg, "out", False)),
        sent_at=msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=UTC),
        body_text=text,
        url=message_url(entity, msg.id),
        raw={"chat_id": chat_id, "msg_id": msg.id, "reply_to": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)},
    )


def classify_dialog(dialog: Any, s: Settings) -> str:
    """Return 'include', 'exclude', or 'needs_check' (group: only if I've posted there)."""
    chat_id = dialog.id
    if chat_id in s.tg_chat_denylist:
        return "exclude"
    if chat_id in s.tg_chat_allowlist:
        return "include"
    entity = dialog.entity
    if getattr(entity, "broadcast", False):
        return "exclude"
    if dialog.is_user:
        if getattr(entity, "bot", False) or getattr(entity, "is_self", False):
            return "exclude"
        return "include"
    if not dialog.is_group:
        return "exclude"
    if getattr(entity, "megagroup", False) and _is_muted(dialog):
        return "exclude"
    return "needs_check"


def _is_muted(dialog: Any) -> bool:
    settings = getattr(getattr(dialog, "dialog", None), "notify_settings", None)
    until = getattr(settings, "mute_until", None)
    if until is None:
        return False
    if isinstance(until, int):
        until = datetime.fromtimestamp(until, UTC)
    return until > datetime.now(UTC)


# --- sync -----------------------------------------------------------------------------


async def _with_flood_wait(make_coro):
    from telethon.errors import FloodWaitError

    for attempt in range(FLOOD_RETRIES):
        try:
            return await make_coro()
        except FloodWaitError as e:
            if attempt == FLOOD_RETRIES - 1:
                raise
            log.warning("FloodWait: sleeping %ss", e.seconds)
            await asyncio.sleep(e.seconds + 1)


async def _fetch_chat(client: Any, entity: Any, chat_id: int, min_id: int, since: datetime) -> tuple[list[IncomingMessage], int]:
    async def run() -> tuple[list[IncomingMessage], int]:
        items: list[IncomingMessage] = []
        max_id = min_id
        if min_id:
            it = client.iter_messages(entity, min_id=min_id, reverse=True)
        else:
            it = client.iter_messages(entity, offset_date=since, reverse=True)
        async for msg in it:
            max_id = max(max_id, msg.id)
            item = to_incoming(msg, chat_id, entity)
            if item:
                items.append(item)
        return items, max_id

    return await _with_flood_wait(run)


async def sync_telegram(client: Any | None = None) -> int:
    """Backfill or incrementally sync Telegram. Returns new messages stored."""
    if client is None:
        async with open_client() as c:
            return await sync_telegram(c)

    s = get_settings()
    me = await client.get_me()
    async with session_scope() as session:
        source = await get_or_create_source(session, "telegram", str(me.id))
        await resolve_person(session, SenderRef(name=display_name(me), telegram_id=me.id), is_me=True)
        source_id = source.id

    since = datetime.now(UTC) - timedelta(days=s.backfill_days)
    total = 0
    synced = 0
    async for dialog in client.iter_dialogs():
        if synced >= s.tg_max_chats:
            break
        if dialog.date and dialog.date < since:
            if getattr(dialog, "pinned", False):
                continue
            break  # unpinned dialogs come newest-first; nothing older is in the window
        verdict = classify_dialog(dialog, s)
        if verdict == "exclude":
            continue
        key = f"chat:{dialog.id}"
        async with session_scope() as session:
            state = await get_state(session, source_id, key)
        if state and state.cursor == SKIP_CURSOR:
            if state.last_synced_at and datetime.now(UTC) - state.last_synced_at < SKIP_RECHECK:
                continue
            state = None
        if verdict == "needs_check" and state is None:
            mine = await _with_flood_wait(lambda: client.get_messages(dialog.entity, from_user="me", limit=1))
            if not mine:
                async with session_scope() as session:
                    await set_state(session, source_id, key, SKIP_CURSOR)
                continue

        min_id = int(state.cursor) if state and state.cursor and state.cursor.isdigit() else 0
        items, max_id = await _fetch_chat(client, dialog.entity, dialog.id, min_id, since)
        async with session_scope() as session:
            source = await session.get(Source, source_id)
            new = await store_messages(session, source, items)
            await set_state(session, source_id, key, str(max_id))
        total += new
        synced += 1
        if new:
            log.info("telegram %s: %d new", display_name(dialog.entity), new)
    return total
