import re

import pytest
from sqlalchemy import func, select

from loops.db.models import Message, Person, Source
from loops.demo.data import THREADS, build_messages
from loops.demo.eval import QUESTIONS, REQUIRED_LOOPS, EvalQuestion, render_report, run_eval, score_loops, score_question
from loops.demo.seed import DemoError, reset_demo, seed_demo
from loops.extract.commitments import apply_waiting_on_rule
from loops.ingest.normalize import get_or_create_source
from tests.fakes import FakeLLM


def test_demo_keys_unique_and_eval_keys_exist():
    keys = [m.key for t in THREADS for m in t.messages]
    assert len(keys) == len(set(keys))
    assert len(QUESTIONS) == 20
    for q in QUESTIONS:
        assert set(q.expected) <= set(keys), q.id
    assert {e.key for e in REQUIRED_LOOPS} <= set(keys)


def test_demo_messages_are_ordered_in_time():
    msgs = build_messages()
    for kind in msgs:
        by_thread = {}
        for m in msgs[kind]:
            by_thread.setdefault(m.thread_external_id, []).append(m.sent_at)
        for times in by_thread.values():
            assert times == sorted(times)


def test_score_question():
    q = EvalQuestion("x", "?", ["a/1", "a/2"])
    r = score_question(q, ["a/1", "b/1"], "ans")
    assert r.passed and r.recall == 0.5 and r.precision == 0.5
    assert not score_question(q, [], "none").passed
    neg = EvalQuestion("n", "?", [])
    assert score_question(neg, [], "nothing").passed
    assert not score_question(neg, ["a/1"], "x").passed
    assert not score_question(q, ["a/1"], "", error="boom").passed


def test_score_loops():
    found = [(e.kind, e.key) for e in REQUIRED_LOOPS[:-1]] + [("they_promised", "team/3"), ("i_promised", "budget/2"), ("postponed", "invoice/1")]
    res = score_loops(found)
    assert res.missed == [f"{REQUIRED_LOOPS[-1].kind} {REQUIRED_LOOPS[-1].key}"]
    assert res.false_positives == ["i_promised budget/2 (should be closed)", "postponed invoice/1"]
    assert res.recall == pytest.approx(10 / 11)


@pytest.mark.db
async def test_seed_refuses_real_sources(db):
    await get_or_create_source(db, "gmail", "real@person.com")
    await db.commit()
    with pytest.raises(DemoError):
        await seed_demo(process=False)


@pytest.mark.db
async def test_seed_reset_and_eval_run(db):
    stats = await seed_demo(process=False)
    total = sum(len(t.messages) for t in THREADS)
    assert stats == {"messages": total, "people_merged": 1}
    robi = (await db.execute(select(Person).where(Person.display_name == "Robi Tesfaye"))).scalar_one()
    assert robi.emails and robi.telegram_ids

    # Reseeding replaces rather than duplicates.
    await seed_demo(process=False, llm=None)
    assert (await db.execute(select(func.count(Message.id)))).scalar() == total

    from loops.ingest.pipeline import process_pending

    await process_pending(FakeLLM(), extract=False)
    await apply_waiting_on_rule(db)
    await db.commit()

    def cite_first(messages, kw):
        evidence = " ".join(m["content"] for m in messages if m["role"] == "system")
        ids = re.findall(r"message_id=(\d+)", evidence)
        return f"Here you go [source: {ids[0]}]" if ids else "Nothing found."

    llm = FakeLLM([cite_first] * 100)
    report = await run_eval(db, llm, mode="router")
    assert len(report["questions"]) == 20
    assert any(r.passed for r in report["questions"])
    loops = report["loops"]
    assert "waiting_on dawit/3" in loops.true_positives and "waiting_on venue/1" in loops.true_positives
    md = render_report(report)
    assert "# Eval report" in md and "Open-loop extraction" in md

    assert await reset_demo() == 2
    assert (await db.execute(select(func.count(Source.id)))).scalar() == 0
    assert (await db.execute(select(func.count(Person.id)))).scalar() == 0
