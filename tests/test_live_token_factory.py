"""Live smoke test against Nebius Token Factory. Runs only when NEBIUS_API_KEY is set."""

import os

import pytest

from loops.core.config import Settings
from loops.llm.token_factory import TokenFactoryClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("NEBIUS_API_KEY"), reason="NEBIUS_API_KEY not set"),
]


@pytest.fixture
async def live_client():
    client = TokenFactoryClient(Settings(chat_system_prefix="/no_think"))
    yield client
    await client.aclose()


async def test_live_chat(live_client):
    msg = await live_client.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=32)
    assert "pong" in (msg.get("content") or "").lower()


async def test_live_embed(live_client):
    data = await live_client._post("embeddings", {"model": live_client.s.embed_model, "input": ["hello"]})
    vec = data["data"][0]["embedding"]
    assert len(vec) > 0
    print("embedding dim:", len(vec))
