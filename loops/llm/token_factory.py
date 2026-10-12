"""Client for Nebius Token Factory (OpenAI-compatible chat + embeddings).

Only open-weight models are called through this client. There is no fallback to any
closed-model API.
"""

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx

from loops.core.config import Settings, get_settings

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_RETRY_STATUSES = {429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    pass


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks that reasoning models may emit."""
    text = _THINK_RE.sub("", text)
    if "</think>" in text:  # opening tag was in the prompt template
        text = text.split("</think>", 1)[1]
    return text.strip()


def parse_json_loose(text: str) -> Any:
    """Parse JSON from model output, tolerating code fences and surrounding prose."""
    text = strip_reasoning(text)
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = min((i for i in (text.find("{"), text.find("[")) if i != -1), default=-1)
    if start == -1:
        raise LLMError(f"no JSON found in model output: {text[:200]!r}")
    end = max(text.rfind("}"), text.rfind("]"))
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"invalid JSON in model output: {e}") from e


class TokenFactoryClient:
    def __init__(self, settings: Settings | None = None, http: httpx.AsyncClient | None = None):
        self.s = settings or get_settings()
        self._http = http or httpx.AsyncClient(
            base_url=self.s.nebius_base_url,
            headers={"Authorization": f"Bearer {self.s.nebius_api_key}"},
            timeout=self.s.llm_timeout_s,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, path: str, payload: dict, attempts: int = 4) -> dict:
        delay = 1.0
        for attempt in range(attempts):
            try:
                r = await self._http.post(path, json=payload)
            except httpx.TransportError as e:
                if attempt == attempts - 1:
                    raise LLMError(f"{path}: {e}") from e
            else:
                if r.status_code < 400:
                    return r.json()
                if r.status_code not in _RETRY_STATUSES or attempt == attempts - 1:
                    raise LLMError(f"{path} -> {r.status_code}: {r.text[:500]}")
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError(f"{path}: retries exhausted")

    def _messages(self, messages: list[dict]) -> list[dict]:
        prefix = self.s.chat_system_prefix
        if not prefix:
            return messages
        if messages and messages[0]["role"] == "system":
            first = {**messages[0], "content": f"{prefix}\n{messages[0]['content']}"}
            return [first, *messages[1:]]
        return [{"role": "system", "content": prefix}, *messages]

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | dict | None = None,
        json_mode: bool = False,
        temperature: float = 0.2,
        max_tokens: int = 2048,
        model: str | None = None,
    ) -> dict:
        """Return the assistant message dict ({'role', 'content', 'tool_calls'?})."""
        payload: dict[str, Any] = {
            "model": model or self.s.chat_model,
            "messages": self._messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = await self._post("chat/completions", payload)
        msg = data["choices"][0]["message"]
        if msg.get("content"):
            msg["content"] = strip_reasoning(msg["content"])
        return msg

    async def chat_json(self, messages: list[dict], **kw) -> Any:
        msg = await self.chat(messages, json_mode=True, temperature=0.0, **kw)
        return parse_json_loose(msg.get("content") or "")

    async def chat_stream(
        self, messages: list[dict], *, temperature: float = 0.2, max_tokens: int = 2048
    ) -> AsyncIterator[str]:
        """Yield content deltas. Reasoning blocks are filtered out."""
        payload = {
            "model": self.s.chat_model,
            "messages": self._messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        in_think = False
        buf = ""
        async with self._http.stream("POST", "chat/completions", json=payload) as r:
            if r.status_code >= 400:
                body = await r.aread()
                raise LLMError(f"chat stream -> {r.status_code}: {body[:500]!r}")
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                choices = json.loads(data).get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta", {}).get("content")
                if not delta:
                    continue
                buf += delta
                # Hold back output while inside a <think> block.
                while buf:
                    if in_think:
                        end = buf.find("</think>")
                        if end == -1:
                            buf = buf[-len("</think>") :]
                            break
                        buf = buf[end + len("</think>") :]
                        in_think = False
                    else:
                        start = buf.find("<think>")
                        if start == -1:
                            # keep a possible partial "<think" tag at the tail
                            safe = len(buf) - (len("<think>") - 1)
                            if safe > 0:
                                yield buf[:safe]
                                buf = buf[safe:]
                            break
                        if start:
                            yield buf[:start]
                        buf = buf[start + len("<think>") :]
                        in_think = True
        if buf and not in_think:
            yield buf

    async def embed(self, texts: list[str], *, model: str | None = None) -> list[list[float]]:
        out: list[list[float]] = []
        bs = self.s.embed_batch_size
        for i in range(0, len(texts), bs):
            batch = [t[:8000] or " " for t in texts[i : i + bs]]
            data = await self._post("embeddings", {"model": model or self.s.embed_model, "input": batch})
            rows = sorted(data["data"], key=lambda d: d["index"])
            for row in rows:
                vec = row["embedding"]
                if len(vec) != self.s.embed_dim:
                    raise LLMError(
                        f"embedding dim {len(vec)} != EMBED_DIM {self.s.embed_dim}; fix EMBED_DIM and re-migrate"
                    )
                out.append(vec)
        return out


_client: TokenFactoryClient | None = None


def get_llm() -> TokenFactoryClient:
    global _client
    if _client is None:
        _client = TokenFactoryClient()
    return _client
