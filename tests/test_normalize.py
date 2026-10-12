import pytest
from sqlalchemy import func, select

from loops.db.models import Message, Person
from loops.ingest.normalize import clean_email_body, get_or_create_source, merge_people, resolve_person, store_messages
from loops.ingest.types import IncomingMessage, SenderRef
from tests.fakes import dt


def test_clean_email_strips_quotes_and_signature():
    body = (
        "Sounds good, I'll send the deck Friday.\n\n"
        "--\nAbebe\nCEO\n\n"
        "On Mon, Oct 5, 2026 at 9:00 AM Robi <robi@x.com> wrote:\n> can you send it?\n"
    )
    assert clean_email_body(body) == "Sounds good, I'll send the deck Friday."


def test_clean_email_two_line_wrote_header():
    body = "Yes.\n\nOn Mon, Oct 5, 2026 at 9:00 AM Robi Tesfaye <\nrobi@x.com> wrote:\n> q"
    assert clean_email_body(body) == "Yes."


def test_clean_email_drops_inline_quotes():
    assert clean_email_body("> old\nnew line\n> older") == "new line"


@pytest.mark.db
async def test_resolve_person_merges_across_sources(db):
    a = await resolve_person(db, SenderRef(name="Robi", email="Robi@X.com"))
    b = await resolve_person(db, SenderRef(name="Robi T", email="robi@x.com", telegram_id=42))
    c = await resolve_person(db, SenderRef(name="", telegram_id=42))
    assert a.id == b.id == c.id
    assert a.emails == ["robi@x.com"]
    assert a.telegram_ids == [42]
    assert "Robi T" in a.aliases


@pytest.mark.db
async def test_real_name_replaces_identifier(db):
    p = await resolve_person(db, SenderRef(name="", email="sam@x.com"))
    assert p.display_name == "sam@x.com"
    p = await resolve_person(db, SenderRef(name="Sam", email="sam@x.com"))
    assert p.display_name == "Sam"


@pytest.mark.db
async def test_store_messages_is_idempotent(db):
    src = await get_or_create_source(db, "telegram", "1")
    items = [
        IncomingMessage("chat1", "m1", SenderRef("Robi", telegram_id=2), False, dt(1), "hi", thread_title="Robi"),
        IncomingMessage("chat1", "m2", SenderRef("Me", telegram_id=1), True, dt(2), "hello", thread_title="Robi"),
    ]
    assert await store_messages(db, src, items) == 2
    assert await store_messages(db, src, items) == 0
    assert (await db.execute(select(func.count(Message.id)))).scalar() == 2
    me = (await db.execute(select(Person).where(Person.is_me))).scalar_one()
    assert me.telegram_ids == [1]


@pytest.mark.db
async def test_merge_people(db):
    a = await resolve_person(db, SenderRef(name="Robi", email="robi@x.com"))
    b = await resolve_person(db, SenderRef(name="Robel", telegram_id=7))
    kept = await merge_people(db, a.id, b.id)
    assert kept.telegram_ids == [7]
    assert "Robel" in kept.aliases
    assert await db.get(Person, b.id) is None
