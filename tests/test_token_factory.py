import json

import httpx
import pytest
import respx

from loops.core.config import Settings
from loops.llm.token_factory import LLMError, TokenFactoryClient, parse_json_loose, strip_reasoning

BASE = "https://tf.test/v1/"


def make_client(**overrides) -> TokenFactoryClient:
    s = Settings(nebius_base_url=BASE, nebius_api_key="k", embed_dim=3, embed_batch_size=2, **overrides)
    return TokenFactoryClient(s)


def test_strip_reasoning():
    assert strip_reasoning("<think>hmm</think> Hello") == "Hello"
    assert strip_reasoning("still thinking</think>Answer") == "Answer"
    assert strip_reasoning("plain") == "plain"


def test_parse_json_loose():
    assert parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_loose('Sure! {"a": [1, 2]} hope that helps') == {"a": [1, 2]}
    with pytest.raises(LLMError):
        parse_json_loose("no json here")


@respx.mock
async def test_chat_adds_system_prefix_and_strips_think():
    route = respx.post(BASE + "chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "<think>x</think>pong"}}]}
        )
    )
    client = make_client(chat_system_prefix="/no_think")
    msg = await client.chat([{"role": "user", "content": "ping"}])
    assert msg["content"] == "pong"
    sent = json.loads(route.calls[0].request.content)
    assert sent["messages"][0] == {"role": "system", "content": "/no_think"}
    assert route.calls[0].request.headers["authorization"] == "Bearer k"


@respx.mock
async def test_chat_retries_on_429(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _no_sleep)
    respx.post(BASE + "chat/completions").mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}),
        ]
    )
    msg = await make_client().chat([{"role": "user", "content": "hi"}])
    assert msg["content"] == "ok"


@respx.mock
async def test_chat_raises_on_400():
    respx.post(BASE + "chat/completions").mock(return_value=httpx.Response(400, text="bad"))
    with pytest.raises(LLMError):
        await make_client().chat([{"role": "user", "content": "hi"}])


@respx.mock
async def test_embed_batches_and_orders():
    def reply(request):
        inputs = json.loads(request.content)["input"]
        data = [{"index": i, "embedding": [float(len(t)), 0.0, 0.0]} for i, t in enumerate(inputs)]
        return httpx.Response(200, json={"data": list(reversed(data))})

    route = respx.post(BASE + "embeddings").mock(side_effect=reply)
    vecs = await make_client().embed(["a", "bb", "ccc"])
    assert [v[0] for v in vecs] == [1.0, 2.0, 3.0]
    assert route.call_count == 2


@respx.mock
async def test_embed_dim_mismatch():
    respx.post(BASE + "embeddings").mock(
        return_value=httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})
    )
    with pytest.raises(LLMError, match="EMBED_DIM"):
        await make_client().embed(["x"])


@respx.mock
async def test_chat_stream_hides_think_block():
    chunks = ["<thi", "nk>secret", " stuff</th", "ink>Hel", "lo ", "world"]
    body = "".join(
        f"data: {json.dumps({'choices': [{'delta': {'content': c}}]})}\n\n" for c in chunks
    ) + "data: [DONE]\n\n"
    respx.post(BASE + "chat/completions").mock(return_value=httpx.Response(200, text=body))
    out = [d async for d in make_client().chat_stream([{"role": "user", "content": "hi"}])]
    assert "".join(out) == "Hello world"


async def _no_sleep(_):
    return None


def test_hnsw_index_switches_to_halfvec_above_2000_dims():
    from loops.db.vector import hnsw_index_sql

    assert "vector_cosine_ops" in hnsw_index_sql(1024)
    assert "halfvec(4096)" in hnsw_index_sql(4096)
