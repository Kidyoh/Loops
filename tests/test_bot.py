from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from telegram import Chat, Message, Update, User

from loops.bot.app import build_app, loops_reply, mark_done
from loops.bot.digest import build_digest, send_digest
from loops.bot.format import format_answer, truncate
from loops.core.config import get_settings
from loops.db.models import Commitment
from tests.seed import seed

OWNER = 1001


def test_format_answer_numbers_and_escapes():
    citations = [
        {"message_id": 5, "source": "gmail", "sender": "Sara", "sent_at": "2026-10-03T08:00:00", "thread_title": "Q&A",
         "url": "https://mail.google.com/x", "app_url": "http://a/messages/5"},
        {"message_id": 7, "source": "telegram", "sender": "Me", "sent_at": "2026-10-01T10:00:00", "thread_title": None,
         "url": None, "app_url": "http://a/messages/7"},
    ]
    out = format_answer("You owe <Sara> a reply [source: 7]. She sent it [source: 5, 7, 999].", citations)
    assert "You owe &lt;Sara&gt; a reply [1]. She sent it [2][1]." in out
    assert '[1] <a href="http://a/messages/7">telegram · Me · 2026-10-01</a>' in out
    assert '[2] <a href="https://mail.google.com/x">gmail · Sara · 2026-10-03 · Q&amp;A</a>' in out
    assert "999" not in out


def test_format_answer_without_citations():
    assert format_answer("Nothing found.", []) == "Nothing found."


def test_truncate_cuts_on_line_boundary():
    text_ = "\n".join(f"line {i}" for i in range(2000))
    out = truncate(text_, 100)
    assert len(out) <= 100 and out.endswith("…(truncated)")


def make_update(user_id: int, text_: str = "who am I waiting on?") -> Update:
    user = User(id=user_id, first_name="X", is_bot=False)
    msg = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=user_id, type="private"), from_user=user, text=text_)
    return Update(update_id=1, message=msg)


def test_bot_ignores_everyone_but_owner():
    app = build_app(token="123:abc", owner_id=OWNER)
    handlers = [h for group in app.handlers.values() for h in group]
    stranger = make_update(42)
    assert not any(h.check_update(stranger) for h in handlers)
    stranger_cmd = make_update(42, "/promises")
    assert not any(h.check_update(stranger_cmd) for h in handlers)
    assert any(h.check_update(make_update(OWNER)) for h in handlers)
    assert any(h.check_update(make_update(OWNER, "/promises")) for h in handlers)


async def add_items(db, ids, now=None):
    now = now or datetime.now(UTC)
    db.add_all([
        Commitment(kind="i_promised", summary="Send comments on the deck", confidence=0.9,
                   source_message_id=ids["2"], due_at=now - timedelta(days=1)),
        Commitment(kind="i_promised", summary="Call the bank", confidence=0.9,
                   source_message_id=ids["1"], due_at=now + timedelta(minutes=5)),
        Commitment(kind="waiting_on", summary="Signed contract from Sara", confidence=0.8, source_message_id=ids["g1"]),
        Commitment(kind="postponed", summary="Investor call", confidence=0.8, source_message_id=ids["3"]),
    ])
    await db.commit()


@pytest.mark.db
async def test_digest_sections(db):
    ids = await seed(db)
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)  # noon in Addis Ababa
    assert await build_digest(db, now) is None
    await add_items(db, ids, now)
    digest = await build_digest(db, now)
    overdue, rest = digest.split("<b>Due today</b>")
    due_today, waiting = rest.split("<b>Waiting on</b>")
    assert "Send comments on the deck" in overdue and "overdue" in overdue
    assert "Call the bank" in due_today
    assert "Signed contract from Sara" in waiting
    assert "Investor call" not in digest


class FakeBot:
    def __init__(self):
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def send_message(self, **kw):
        self.sent.append(kw)


@pytest.mark.db
async def test_send_digest(db, monkeypatch):
    ids = await seed(db)
    monkeypatch.setattr(get_settings(), "bot_token", "t")
    monkeypatch.setattr(get_settings(), "owner_telegram_id", OWNER)
    bot = FakeBot()
    assert await send_digest(bot) is False  # nothing open
    await add_items(db, ids)
    assert await send_digest(bot) is True
    assert bot.sent[0]["chat_id"] == OWNER and bot.sent[0]["parse_mode"] == "HTML"


@pytest.mark.db
async def test_loops_reply_and_done(db):
    ids = await seed(db)
    await add_items(db, ids)
    text_, button_ids = await loops_reply("i_promised")
    assert "I promised" in text_ and "Send comments on the deck" in text_
    assert len(button_ids) == 2
    reply = await mark_done(button_ids[0])
    assert reply.startswith("✅ Done")
    assert "already done" in await mark_done(button_ids[0])
    assert "No item" in await mark_done(99999)
    text_, button_ids = await loops_reply("i_promised")
    assert len(button_ids) == 1


@pytest.mark.db
async def test_sync_all_skips_when_locked(db):
    from loops.worker.jobs import SYNC_LOCK_ID, sync_all

    await db.execute(text("SELECT pg_advisory_lock(:id)"), {"id": SYNC_LOCK_ID})
    try:
        assert await sync_all() == {"skipped": "sync already running"}
    finally:
        await db.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": SYNC_LOCK_ID})


def test_scheduler_jobs():
    from loops.worker.main import build_scheduler

    sched = build_scheduler()
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {"sync", "digest"}
    assert str(jobs["digest"].trigger.timezone) == get_settings().timezone
