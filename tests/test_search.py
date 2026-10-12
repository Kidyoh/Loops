import pytest

from loops.agent.search import find_people, hybrid_search
from tests.fakes import FakeLLM, dt
from tests.seed import seed

pytestmark = pytest.mark.db


async def test_keyword_search_and_lines(db):
    ids = await seed(db)
    hits = await hybrid_search(db, FakeLLM(), "Habesha pitch deck", k=3)
    assert hits[0].source == "telegram"
    assert ids["1"] in hits[0].message_ids
    assert any(f"[message_id={ids['1']}]" in line for line in hits[0].lines)


async def test_filters(db):
    await seed(db)
    llm = FakeLLM()
    assert all(h.source == "gmail" for h in await hybrid_search(db, llm, "contract budget", source="gmail"))
    sara = await find_people(db, "sara")
    hits = await hybrid_search(db, llm, "budget", person_ids=[p.id for p in sara])
    assert hits and all(h.source == "gmail" for h in hits)
    assert await hybrid_search(db, llm, "budget", after=dt(10)) == []


async def test_find_people_by_alias_and_email(db):
    await seed(db)
    assert [p.display_name for p in await find_people(db, "robi")] == ["Robi Tesfaye"]
    assert [p.display_name for p in await find_people(db, "sara@x")] == ["Sara Bekele"]
    assert await find_people(db, "nobody") == []
