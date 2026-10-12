from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text

from loops.core.config import Settings
from loops.db.models import Chunk, Message, SyncState
from loops.ingest.pipeline import process_pending
from loops.ingest.telegram import classify_dialog, message_url, sync_telegram, to_incoming
from tests.fakes import FakeLLM, FakeTelegramClient, tg_dialog, tg_group, tg_msg, tg_user

NOW = datetime.now(UTC)


def test_classify_dialog():
    s = Settings(tg_chat_allowlist=[-5], tg_chat_denylist=[3])
    robi = tg_user(2, "Robi")
    assert classify_dialog(tg_dialog(2, robi, NOW, is_user=True), s) == "include"
    assert classify_dialog(tg_dialog(3, tg_user(3, "X"), NOW, is_user=True), s) == "exclude"
    assert classify_dialog(tg_dialog(4, tg_user(4, "Bot", bot=True), NOW, is_user=True), s) == "exclude"
    assert classify_dialog(tg_dialog(-1, tg_group(1, "News", broadcast=True), NOW), s) == "exclude"
    assert classify_dialog(tg_dialog(-2, tg_group(2, "Big", megagroup=True), NOW, is_group=True, muted=True), s) == "exclude"
    assert classify_dialog(tg_dialog(-3, tg_group(3, "Team"), NOW, is_group=True), s) == "needs_check"
    assert classify_dialog(tg_dialog(-5, tg_group(5, "Muted", megagroup=True), NOW, is_group=True, muted=True), s) == "include"


def test_message_url():
    assert message_url(tg_group(9, "Pub", megagroup=True, username="pubgroup"), 5) == "https://t.me/pubgroup/5"
    assert message_url(tg_group(9, "Priv", megagroup=True), 5) == "https://t.me/c/9/5"
    assert message_url(tg_user(2, "Robi", username="robi"), 5) is None


def test_to_incoming_skips_empty():
    robi = tg_user(2, "Robi", "T")
    assert to_incoming(tg_msg(1, NOW, "", robi), 2, robi) is None
    item = to_incoming(tg_msg(1, NOW, " hi ", robi), 2, robi)
    assert item.body_text == "hi" and item.sender.name == "Robi T" and item.thread_title == "Robi T"


def build_client():
    me = tg_user(1, "Abebe", is_self=True)
    robi = tg_user(2, "Robi")
    team = tg_group(10, "Habesha team")
    lurk = tg_group(11, "Lurk group")
    t = NOW - timedelta(days=2)
    messages = {
        2: [
            tg_msg(1, t, "Can you review the Habesha pitch deck?", robi),
            tg_msg(2, t + timedelta(minutes=1), "Yes, I'll send comments tomorrow", me, out=True),
        ],
        -10: [
            tg_msg(1, t, "Standup moved to 10", robi),
            tg_msg(2, t + timedelta(minutes=5), "ok", me, out=True),
        ],
        -11: [tg_msg(1, t, "spam", robi)],
    }
    dialogs = [
        tg_dialog(2, robi, t, is_user=True),
        tg_dialog(-10, team, t, is_group=True),
        tg_dialog(-11, lurk, t, is_group=True),
        tg_dialog(99, tg_user(99, "Old"), NOW - timedelta(days=400), is_user=True),
    ]
    return FakeTelegramClient(me, dialogs, messages)


@pytest.mark.db
async def test_sync_then_process_end_to_end(db):
    client = build_client()
    assert await sync_telegram(client) == 4  # lurk group skipped, old dialog outside backfill
    assert await sync_telegram(client) == 0  # incremental: nothing new

    client.messages[2].append(tg_msg(3, NOW, "Thanks! Also the budget sheet please", client.dialogs[0].entity))
    assert await sync_telegram(client) == 1

    stats = await process_pending(FakeLLM(), extract=False)
    assert stats["chunks"] >= 2 and stats["embedded"] == stats["chunks"]

    await db.rollback()
    pending = (await db.execute(select(func.count()).where(Message.chunked_at.is_(None)))).scalar()
    assert pending == 0
    hits = (
        await db.execute(select(Chunk.text).where(Chunk.tsv.op("@@")(func.plainto_tsquery("english", "pitch deck"))))
    ).scalars().all()
    assert hits and "Habesha pitch deck" in hits[0]
    skip = (await db.execute(select(SyncState.cursor).where(SyncState.key == "chat:-11"))).scalar()
    assert skip == "skip"
    nearest = (
        await db.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL"))
    ).scalar()
    assert nearest == stats["chunks"]
