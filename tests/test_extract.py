import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from loops.db.models import Commitment, Message, Person
from loops.extract.commitments import apply_waiting_on_rule, end_of_day, extract_pending
from loops.ingest.normalize import get_or_create_source, store_messages
from loops.ingest.types import IncomingMessage
from tests.fakes import FakeLLM, dt
from tests.seed import ME_TG, ROBI_TG, SARA, seed

pytestmark = pytest.mark.db


def by_title(replies: dict):
    """Fake extraction reply chosen by which thread the prompt is about."""

    def reply(messages, kw):
        prompt = messages[0]["content"]
        for title, data in replies.items():
            if f'thread "{title}"' in prompt:
                return json.dumps(data(prompt) if callable(data) else data)
        return json.dumps({"commitments": [], "resolved": []})

    return reply


def test_end_of_day():
    due = end_of_day("2026-10-02", "Africa/Addis_Ababa")
    assert due == datetime(2026, 10, 2, 20, 59, 59, tzinfo=UTC)
    assert end_of_day("soon", "UTC") is None


async def test_extract_validates_and_stores(db):
    ids = await seed(db)
    robi = {
        "commitments": [
            {"kind": "i_promised", "person": "Robi", "summary": "Send Robi detailed comments on the pitch deck",
             "due": "2026-10-02", "evidence_message_id": ids["2"], "confidence": 0.9},
            {"kind": "postponed", "person": "Robi Tesfaye", "summary": "Investor call moved to next week",
             "due": None, "evidence_message_id": ids["3"], "confidence": 0.8},
            # i_promised must come from Me
            {"kind": "i_promised", "person": "Robi", "summary": "bogus", "evidence_message_id": ids["1"], "confidence": 0.9},
            # below threshold
            {"kind": "they_promised", "person": "Robi", "summary": "weak", "evidence_message_id": ids["3"], "confidence": 0.3},
            # evidence outside this thread
            {"kind": "i_promised", "person": "Robi", "summary": "wrong", "evidence_message_id": ids["g1"], "confidence": 0.9},
            {"kind": "nonsense", "summary": "x", "evidence_message_id": ids["2"], "confidence": 1},
        ],
        "resolved": [],
    }
    llm = FakeLLM([by_title({"Robi Tesfaye": robi})] * 3)
    stats = await extract_pending(db, llm)
    assert stats["threads"] == 3 and stats["created"] == 2 and stats["failed"] == 0

    rows = (await db.execute(select(Commitment).order_by(Commitment.id))).scalars().all()
    assert [(c.kind, c.source_message_id) for c in rows] == [("i_promised", ids["2"]), ("postponed", ids["3"])]
    robi_id = (await db.execute(select(Person.id).where(Person.display_name == "Robi Tesfaye"))).scalar()
    assert all(c.person_id == robi_id for c in rows)
    assert rows[0].due_at == end_of_day("2026-10-02", "Africa/Addis_Ababa")

    # Nothing pending: no further model calls.
    calls = len(llm.calls)
    assert (await extract_pending(db, llm))["threads"] == 0
    assert len(llm.calls) == calls

    # Prompt marks unprocessed messages NEW and carries message ids.
    robi_prompt = next(c["messages"][0]["content"] for c in llm.calls if 'thread "Robi Tesfaye"' in c["messages"][0]["content"])
    assert f"NEW [message_id={ids['2']}]" in robi_prompt


async def test_new_message_resolves_open_item(db):
    ids = await seed(db)
    first = {"commitments": [{"kind": "i_promised", "person": "Robi", "summary": "Send comments",
                              "evidence_message_id": ids["2"], "confidence": 0.9}], "resolved": []}
    await extract_pending(db, FakeLLM([by_title({"Robi Tesfaye": first})] * 3))
    promise = (await db.execute(select(Commitment))).scalar_one()

    tg = await get_or_create_source(db, "telegram", "1")
    await store_messages(db, tg, [IncomingMessage("2", "4", ME_TG, True, dt(2, 12), "Here are my comments on the deck", "Robi Tesfaye")])
    await db.commit()
    new_id = (await db.execute(select(Message.id).where(Message.external_id == "4"))).scalar()

    def second(prompt):
        assert f"[id={promise.id}] i_promised: Send comments" in prompt
        assert f"NEW [message_id={new_id}]" in prompt and f"NEW [message_id={ids['2']}]" not in prompt
        return {"commitments": [], "resolved": [{"id": promise.id, "evidence_message_id": new_id}]}

    stats = await extract_pending(db, FakeLLM([by_title({"Robi Tesfaye": second})]))
    assert stats["resolved"] == 1
    await db.refresh(promise)
    assert promise.status == "done"


async def test_bad_model_output_is_retried_later(db):
    await seed(db)
    stats = await extract_pending(db, FakeLLM(["not json at all"] * 3))
    assert stats["failed"] == 3
    pending = (await db.execute(select(func.count()).where(Message.extracted_at.is_(None)))).scalar()
    assert pending == 5


async def test_waiting_on_rule(db):
    ids = await seed(db)
    now = dt(10)
    stats = await apply_waiting_on_rule(db, now=now)
    # Contract email (mine, asks Sara, 7 days old) qualifies; my Telegram promise isn't last in its thread.
    assert stats == {"rule_created": 1, "rule_closed": 0}
    item = (await db.execute(select(Commitment))).scalar_one()
    sara = (await db.execute(select(Person.id).where(Person.display_name == "Sara Bekele"))).scalar()
    assert item.kind == "waiting_on" and item.origin == "rule" and item.person_id == sara
    assert item.source_message_id == ids["g1"]
    assert (await apply_waiting_on_rule(db, now=now))["rule_created"] == 0

    gm = await get_or_create_source(db, "gmail", "abebe@example.com")
    await store_messages(db, gm, [IncomingMessage("t1", "g9", SARA, False, dt(11), "Signed contract attached", "Contract")])
    stats = await apply_waiting_on_rule(db, now=now + timedelta(days=2))
    assert stats["rule_closed"] == 1
    await db.refresh(item)
    assert item.status == "done"


async def test_waiting_on_rule_ignores_statements_and_recent(db):
    tg = await get_or_create_source(db, "telegram", "1")
    await store_messages(db, tg, [
        IncomingMessage("5", "1", ME_TG, True, dt(1), "Thanks, all good.", "Robi"),
        IncomingMessage("6", "1", ME_TG, True, dt(9), "Can you confirm the venue?", "Sam"),
    ])
    assert (await apply_waiting_on_rule(db, now=dt(10)))["rule_created"] == 0
