import json

import httpx
import pytest
from sqlalchemy import select

from loops.core.config import get_settings
from loops.db.models import Commitment
from tests.fakes import FakeLLM
from tests.seed import seed

pytestmark = pytest.mark.db


@pytest.fixture
async def client(db, monkeypatch):
    from loops.api import main

    monkeypatch.setattr(get_settings(), "web_password", "pw")
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def login(client):
    r = await client.post("/login", json={"password": "pw"})
    assert r.status_code == 200


async def test_auth_required(client):
    assert (await client.get("/commitments")).status_code == 401
    assert (await client.post("/login", json={"password": "nope"})).status_code == 401
    assert (await client.get("/commitments", headers={"Authorization": "Bearer pw"})).status_code == 200


async def test_commitments_and_patch(client, db):
    ids = await seed(db)
    db.add(Commitment(kind="i_promised", summary="Send comments on the deck", confidence=0.9, source_message_id=ids["2"]))
    await db.commit()
    await login(client)
    items = (await client.get("/commitments", params={"kind": "i_promised"})).json()
    assert [c["summary"] for c in items] == ["Send comments on the deck"]
    assert items[0]["source"] == "telegram"
    r = await client.patch(f"/commitments/{items[0]['id']}", json={"status": "done"})
    assert r.json()["status"] == "done"
    assert (await client.get("/commitments")).json() == []
    assert len((await client.get("/commitments", params={"status": "any"})).json()) == 1
    assert (await client.patch("/commitments/999", json={"status": "done"})).status_code == 404


async def test_message_context_and_sources(client, db):
    ids = await seed(db)
    await login(client)
    data = (await client.get(f"/messages/{ids['2']}")).json()
    assert data["message"]["sender"] == "Me"
    assert [m["id"] for m in data["context"]] == [ids["1"], ids["2"], ids["3"]]
    sources = (await client.get("/sources")).json()
    assert {s["kind"]: s["messages"] for s in sources} == {"telegram": 3, "gmail": 2}


async def test_ask_streams_sse(client, db, monkeypatch):
    ids = await seed(db)
    tool = {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "type": "function", "function": {"name": "search_messages", "arguments": '{"query": "budget"}'}}]}
    fake = FakeLLM([tool, f"Sara sent budget numbers [source: {ids['g2']}]"])
    monkeypatch.setattr("loops.api.main.get_llm", lambda: fake)
    await login(client)
    events = []
    async with client.stream("POST", "/ask", json={"question": "budget?"}) as r:
        event = None
        async for line in r.aiter_lines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                events.append((event, json.loads(line[5:].strip())))
    kinds = [e for e, _ in events]
    assert kinds[0] == "tool" and "token" in kinds and kinds[-2:] == ["citations", "done"]
    assert events[-2][1]["items"][0]["message_id"] == ids["g2"]


async def test_delete_source_wipes_rows(client, db):
    ids = await seed(db)
    await login(client)
    sources = (await client.get("/sources")).json()
    gmail_id = next(s["id"] for s in sources if s["kind"] == "gmail")
    assert (await client.delete(f"/sources/{gmail_id}")).status_code == 200
    assert (await client.get(f"/messages/{ids['g1']}")).status_code == 404
    assert (await client.get(f"/messages/{ids['1']}")).status_code == 200
