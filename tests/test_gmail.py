import base64
import json
from datetime import UTC, datetime

import httpx
import pytest
import respx
from sqlalchemy import func, select

from loops.core.crypto import encrypt
from loops.db.models import Message, Person, Source, SyncState, Thread
from loops.ingest.gmail import API, TOKEN_URL, extract_body, html_to_text, parse_message, sync_gmail
from loops.ingest.normalize import get_or_create_source, resolve_person, suggest_merges
from loops.ingest.types import SenderRef

ME = "abebe@example.com"


def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def gmail_msg(mid, thread, frm, to, subject, body, ts, labels=("INBOX",), html_body=None):
    parts = [{"mimeType": "text/plain", "body": {"data": b64(body)}}] if body is not None else []
    if html_body:
        parts.append({"mimeType": "text/html", "body": {"data": b64(html_body)}})
    return {
        "id": mid,
        "threadId": thread,
        "labelIds": list(labels),
        "snippet": "snip",
        "internalDate": str(int(ts.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": frm},
                {"name": "To", "value": to},
                {"name": "Subject", "value": subject},
            ],
            "parts": parts,
        },
    }


T0 = datetime(2026, 10, 1, 9, tzinfo=UTC)


def test_parse_message_from_me_and_cleaning():
    raw = gmail_msg(
        "m1", "t1", f"Abebe <{ME}>", "Robi Tesfaye <robi@x.com>", "Habesha pitch",
        "I'll send the revised deck Friday.\n\nOn Mon, Robi wrote:\n> can you?", T0, labels=("SENT",),
    )
    item = parse_message(raw, ME)
    assert item.is_from_me
    assert item.body_text == "I'll send the revised deck Friday."
    assert item.participants[0].email == "robi@x.com"
    assert item.thread_title == "Habesha pitch"
    assert item.sent_at == T0


def test_parse_message_skips_promotions():
    raw = gmail_msg("m1", "t1", "Shop <a@b.c>", ME, "Sale", "50% off", T0, labels=("CATEGORY_PROMOTIONS",))
    assert parse_message(raw, ME) is None


def test_html_fallback():
    raw = gmail_msg("m1", "t1", "R <r@x.com>", ME, "s", None, T0, html_body="<p>Hello&nbsp;<b>there</b></p><br>Bye")
    assert extract_body(raw["payload"]) == "Hello there\n\nBye"
    assert html_to_text("<style>x{}</style><div>a</div><div>b</div>") == "a\nb"


def mock_google(router, messages, history=None, history_status=200, profile_history="100"):
    router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"access_token": "at"}))
    router.get(API + "profile").mock(
        return_value=httpx.Response(200, json={"emailAddress": ME, "historyId": profile_history})
    )
    router.get(API + "messages").mock(
        return_value=httpx.Response(200, json={"messages": [{"id": m["id"]} for m in messages]})
    )
    by_id = {m["id"]: m for m in messages}
    router.get(url__regex=API.replace(".", r"\.") + r"messages/(?P<mid>\w+)").mock(
        side_effect=lambda request, mid: httpx.Response(200, json=by_id[mid]) if mid in by_id else httpx.Response(404)
    )
    router.get(API + "history").mock(return_value=httpx.Response(history_status, json=history or {}))


@pytest.mark.db
async def test_gmail_backfill_then_history(db):
    src = await get_or_create_source(db, "gmail", ME)
    src.credentials = encrypt(b"refresh")
    await db.commit()

    m1 = gmail_msg("a1", "t1", "Robi Tesfaye <robi@x.com>", ME, "Habesha pitch", "Can you send the deck?", T0)
    m2 = gmail_msg("a2", "t1", f"Abebe <{ME}>", "robi@x.com", "Re: Habesha pitch", "Yes, Friday.", T0.replace(hour=10), labels=("SENT",))
    with respx.mock(assert_all_called=False) as router:
        mock_google(router, [m1, m2])
        assert await sync_gmail() == 2

    cursor = (await db.execute(select(SyncState.cursor).where(SyncState.key == "history_id"))).scalar()
    assert cursor == "100"

    m3 = gmail_msg("a3", "t1", "Robi Tesfaye <robi@x.com>", ME, "Re: Habesha pitch", "Great, thanks", T0.replace(hour=11))
    history = {"historyId": "120", "history": [{"messagesAdded": [{"message": {"id": "a3"}}]}]}
    with respx.mock(assert_all_called=False) as router:
        mock_google(router, [m1, m2, m3], history=history)
        assert await sync_gmail() == 1

    await db.rollback()
    assert (await db.execute(select(func.count(Message.id)))).scalar() == 3
    assert (await db.execute(select(func.count(Thread.id)))).scalar() == 1
    me = (await db.execute(select(Person).where(Person.is_me))).scalar_one()
    assert ME in me.emails
    cursor = (await db.execute(select(SyncState.cursor).where(SyncState.key == "history_id"))).scalar()
    assert cursor == "120"


@pytest.mark.db
async def test_gmail_expired_history_relists(db):
    src = await get_or_create_source(db, "gmail", ME)
    src.credentials = encrypt(b"refresh")
    db.add(SyncState(source_id=src.id, key="history_id", cursor="5"))
    await db.commit()
    m1 = gmail_msg("a1", "t1", "Robi <robi@x.com>", ME, "Hi", "hello", T0)
    with respx.mock(assert_all_called=False) as router:
        mock_google(router, [m1], history_status=404, profile_history="200")
        assert await sync_gmail() == 1
    await db.rollback()
    cursor = (await db.execute(select(SyncState.cursor).where(SyncState.key == "history_id"))).scalar()
    assert cursor == "200"


@pytest.mark.db
async def test_suggest_merges_across_sources(db):
    a = await resolve_person(db, SenderRef(name="Robi Tesfaye", email="robi@x.com"))
    b = await resolve_person(db, SenderRef(name="Robi Tesfaye", telegram_id=55))
    await resolve_person(db, SenderRef(name="Sam", email="sam@x.com"))
    await resolve_person(db, SenderRef(name="Sam", telegram_id=56))  # single word: too ambiguous
    pairs = await suggest_merges(db)
    assert [(p.id, q.id) for p, q in pairs] == [(a.id, b.id)]
