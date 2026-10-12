import json

import pytest

from loops.agent.agent import extract_citation_ids, run_agent
from tests.fakes import FakeLLM
from tests.seed import seed


def test_extract_citation_ids():
    text = "You promised X [source: 12]. Also Y [source: 3, 12, 40]."
    assert extract_citation_ids(text) == [12, 3, 40]


def tool_call(name, args, id="c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


async def collect(gen):
    return [e async for e in gen]


@pytest.mark.db
async def test_tool_loop_cites_only_seen_messages(db):
    ids = await seed(db)
    answer = f"You said you'd send comments tomorrow [source: {ids['2']}]. Bogus [source: 99999]."
    llm = FakeLLM([tool_call("search_messages", {"query": "Habesha pitch"}), answer])
    events = await collect(run_agent(db, llm, "What did I tell Robi about the pitch?", mode="tools"))
    assert events[0] == {"type": "tool", "name": "search_messages", "args": {"query": "Habesha pitch"}}
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text == answer
    cites = events[-1]["items"]
    assert [c["message_id"] for c in cites] == [ids["2"]]
    assert cites[0]["source"] == "telegram" and cites[0]["sender"] == "Me"
    # tool result went back to the model with message ids in it
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool" and f"message_id={ids['2']}" in tool_msg["content"]


@pytest.mark.db
async def test_answer_without_tools_falls_back_to_router(db):
    ids = await seed(db)
    plan = json.dumps({"intent": "search", "kind": None, "person": "Sara", "after": None, "before": None, "query": "signed contract"})
    final = f"You asked Sara for the signed contract [source: {ids['g1']}]"
    llm = FakeLLM(["I think you're waiting on Sara.", plan, final])
    events = await collect(run_agent(db, llm, "Who am I waiting on?", mode="tools"))
    tools = [e["name"] for e in events if e["type"] == "tool"]
    assert tools == ["search_messages"]
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert text.strip() == final
    assert [c["message_id"] for c in events[-1]["items"]] == [ids["g1"]]


@pytest.mark.db
async def test_router_says_so_when_nothing_found(db):
    await seed(db)
    plan = json.dumps({"intent": "search", "person": "Nobody Known", "query": "zebra"})
    llm = FakeLLM([plan])
    events = await collect(run_agent(db, llm, "What did Nobody say about zebras?", mode="router"))
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "couldn't find" in text
    assert events[-1] == {"type": "citations", "items": []}
