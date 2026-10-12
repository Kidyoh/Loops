"""Query agent: Nemotron with tool calling, plus a fixed router as a fallback.

Every claim in an answer must cite `[source: <message_id>]`. Citations pointing at
messages the model never saw in a tool result are dropped.
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loops.agent.tools import TOOL_SCHEMAS, ToolRunner
from loops.core.config import get_settings
from loops.db.models import COMMITMENT_KINDS, Message, Person, Source, Thread
from loops.llm.token_factory import LLMError

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
CITATION_RE = re.compile(r"\[source:\s*([\d,\s]+)\]")

SYSTEM_PROMPT = """You answer questions about the user's own Gmail and Telegram messages.
Now: {now} ({tz}). "This month" means since {month_start}; "this week" means since {week_start}.

Rules:
- Use the tools to find evidence before answering. Never answer from memory.
- Every factual claim must end with a citation like [source: 123] using the message_id from tool results. Several sources: [source: 123, 456].
- If nothing relevant is found, say so plainly. Do not guess.
- "I", "me" and "my" refer to the user. In tool results, "Me" is the user.
- For promises, waiting-on and postponed questions, call list_commitments first, then search_messages if it is empty.
- Be concise. Prefer short bullet lists for multiple items, each with its citation."""

ROUTER_PROMPT = """Classify the user's question about their messages. Reply with JSON only:
{{"intent": "commitments" | "search", "kind": one of {kinds} or null, "person": string or null,
"after": "YYYY-MM-DD" or null, "before": "YYYY-MM-DD" or null, "query": search text}}
Today is {today}. "This month" starts {month_start}."""

ANSWER_PROMPT = """Answer the question using only the evidence below. Cite every claim as [source: <message_id>].
If the evidence does not answer the question, say so.

Evidence:
{evidence}"""


def _time_context() -> dict:
    tz = get_settings().timezone
    now = datetime.now(ZoneInfo(tz))
    return {
        "now": now.strftime("%A %Y-%m-%d %H:%M"),
        "today": now.date().isoformat(),
        "tz": tz,
        "month_start": now.date().replace(day=1).isoformat(),
        "week_start": (now.date() - timedelta(days=now.weekday())).isoformat(),
    }


def extract_citation_ids(text: str) -> list[int]:
    ids: list[int] = []
    for group in CITATION_RE.findall(text):
        for part in group.split(","):
            part = part.strip()
            if part.isdigit() and int(part) not in ids:
                ids.append(int(part))
    return ids


async def build_citations(session: AsyncSession, ids: list[int], allowed: set[int]) -> list[dict]:
    ids = [i for i in ids if i in allowed]
    if not ids:
        return []
    rows = (
        await session.execute(
            select(Message, Thread.title, Source.kind, Person.display_name)
            .join(Thread, Thread.id == Message.thread_id)
            .join(Source, Source.id == Thread.source_id)
            .outerjoin(Person, Person.id == Message.sender_id)
            .where(Message.id.in_(ids))
        )
    ).all()
    base = get_settings().public_base_url.rstrip("/")
    by_id = {
        m.id: {
            "message_id": m.id,
            "source": kind,
            "thread_id": m.thread_id,
            "thread_title": title,
            "sender": "Me" if m.is_from_me else name,
            "sent_at": m.sent_at.isoformat(),
            "snippet": m.body_text[:200],
            "url": m.url,
            "app_url": f"{base}/messages/{m.id}",
        }
        for m, title, kind, name in rows
    }
    return [by_id[i] for i in ids if i in by_id]


def _word_chunks(text: str) -> list[str]:
    return re.findall(r"\S+\s*|\s+", text)


async def run_agent(
    session: AsyncSession, llm, question: str, history: list[dict] | None = None, mode: str | None = None
) -> AsyncIterator[dict]:
    """Yield events: {"type": "tool"|"token"|"citations"|"error", ...}."""
    mode = mode or get_settings().agent_mode
    runner = ToolRunner(session, llm)
    answer = ""
    if mode == "tools":
        try:
            async for event in _tool_loop(runner, llm, question, history or []):
                if event["type"] == "answer":
                    answer = event["text"]
                else:
                    yield event
        except LLMError as e:
            log.warning("tool calling failed, falling back to router: %s", e)
            answer = ""
        if answer:
            for piece in _word_chunks(answer):
                yield {"type": "token", "text": piece}
    if not answer:
        async for event in _router(runner, llm, question):
            if event["type"] == "token":
                answer += event["text"]
            yield event

    citations = await build_citations(session, extract_citation_ids(answer), runner.seen_message_ids)
    yield {"type": "citations", "items": citations}


async def _tool_loop(runner: ToolRunner, llm, question: str, history: list[dict]) -> AsyncIterator[dict]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(**_time_context())},
        *history,
        {"role": "user", "content": question},
    ]
    for _ in range(MAX_TOOL_ROUNDS):
        msg = await llm.chat(messages, tools=TOOL_SCHEMAS, max_tokens=1500)
        calls = msg.get("tool_calls") or []
        if not calls:
            if not _used_tools(messages):
                # Answered without looking anything up: don't trust it, use the router.
                return
            yield {"type": "answer", "text": msg.get("content") or ""}
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool", "name": name, "args": args}
            result = await runner.run(name, args)
            messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})

    messages.append({"role": "user", "content": "Answer now with what you have found, citing sources."})
    msg = await llm.chat(messages, max_tokens=1500)
    yield {"type": "answer", "text": msg.get("content") or ""}


def _used_tools(messages: list[dict]) -> bool:
    return any(m["role"] == "tool" for m in messages)


async def _router(runner: ToolRunner, llm, question: str) -> AsyncIterator[dict]:
    """Fixed pipeline: classify intent, call the matching tool(s), answer from the evidence."""
    ctx = _time_context()
    try:
        plan = await llm.chat_json(
            [
                {"role": "system", "content": ROUTER_PROMPT.format(kinds=list(COMMITMENT_KINDS), **ctx)},
                {"role": "user", "content": question},
            ]
        )
    except LLMError:
        plan = {}
    if not isinstance(plan, dict):
        plan = {}
    kind = plan.get("kind") if plan.get("kind") in COMMITMENT_KINDS else None
    person, after, before = plan.get("person"), plan.get("after"), plan.get("before")
    query = plan.get("query") or question

    evidence = []
    if plan.get("intent") == "commitments" or kind:
        yield {"type": "tool", "name": "list_commitments", "args": {"kind": kind, "person": person}}
        result = await runner.run("list_commitments", {"kind": kind, "person": person, "after": after, "before": before})
        if not result.startswith("No "):
            evidence.append(result)
    yield {"type": "tool", "name": "search_messages", "args": {"query": query, "person": person}}
    result = await runner.run("search_messages", {"query": query, "person": person, "after": after, "before": before})
    if not result.startswith("No "):
        evidence.append(result)

    if not evidence:
        yield {"type": "token", "text": "I couldn't find anything in your messages about that."}
        return
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT.format(**ctx)},
        {"role": "system", "content": ANSWER_PROMPT.format(evidence="\n\n".join(evidence))},
        {"role": "user", "content": question},
    ]
    async for piece in llm.chat_stream(messages, max_tokens=1500):
        yield {"type": "token", "text": piece}
