"""Tools the query agent can call. Results are plain text, every line tagged with a message id."""

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from loops.agent.queries import get_thread_messages, list_commitments
from loops.agent.search import find_people, format_message_line, hybrid_search
from loops.core.config import get_settings
from loops.db.models import COMMITMENT_KINDS, COMMITMENT_STATUSES

_DATE = {"type": "string", "description": "ISO date (YYYY-MM-DD) or datetime"}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "search_messages",
            "description": "Hybrid semantic + keyword search over Gmail and Telegram messages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look for, in natural language"},
                    "person": {"type": "string", "description": "Only threads involving this person (name, handle or email)"},
                    "source": {"type": "string", "enum": ["gmail", "telegram"]},
                    "after": _DATE,
                    "before": _DATE,
                    "k": {"type": "integer", "description": "Max results (default 8)"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_commitments",
            "description": (
                "List extracted open loops. kinds: i_promised (I said I'd do something), they_promised "
                "(someone promised me), waiting_on (I asked and got no reply), postponed (moved later)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(COMMITMENT_KINDS)},
                    "person": {"type": "string"},
                    "status": {"type": "string", "enum": [*COMMITMENT_STATUSES, "any"], "description": "default open"},
                    "after": _DATE,
                    "before": _DATE,
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_thread",
            "description": "Read messages of a thread, optionally centred on one message.",
            "parameters": {
                "type": "object",
                "properties": {
                    "thread_id": {"type": "integer"},
                    "around_message_id": {"type": "integer"},
                },
                "required": ["thread_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_person",
            "description": "Resolve a name, handle or email to known people.",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    },
]


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(get_settings().timezone))
    return dt


class ToolRunner:
    def __init__(self, session: AsyncSession, llm):
        self.session = session
        self.llm = llm
        # Message ids the model has actually seen; only these may be cited.
        self.seen_message_ids: set[int] = set()

    async def run(self, name: str, args: dict[str, Any]) -> str:
        fn = getattr(self, f"_tool_{name}", None)
        if fn is None:
            return f"error: unknown tool {name}"
        try:
            return await fn(**args)
        except (TypeError, ValueError) as e:
            return f"error: bad arguments for {name}: {e}"

    async def _person_ids(self, person: str | None) -> list[int] | None:
        if not person:
            return None
        people = await find_people(self.session, person)
        return [p.id for p in people] or [-1]  # -1: named person unknown, match nothing

    async def _tool_search_messages(self, query, person=None, source=None, after=None, before=None, k=8) -> str:
        hits = await hybrid_search(
            self.session,
            self.llm,
            query,
            person_ids=await self._person_ids(person),
            source=source,
            after=parse_date(after),
            before=parse_date(before),
            k=min(int(k), 20),
        )
        if not hits:
            return "No matching messages."
        blocks = []
        for h in hits:
            self.seen_message_ids.update(h.message_ids)
            header = f"--- {h.source} thread_id={h.thread_id} \"{h.thread_title or ''}\""
            blocks.append("\n".join([header, *h.lines]))
        return "\n".join(blocks)

    async def _tool_list_commitments(self, kind=None, person=None, status="open", after=None, before=None) -> str:
        items = await list_commitments(
            self.session,
            kind=kind,
            status=None if status == "any" else status,
            person_ids=await self._person_ids(person),
            after=parse_date(after),
            before=parse_date(before),
        )
        if not items:
            return "No commitments found."
        lines = []
        for c in items:
            if c.source_message_id:
                self.seen_message_ids.add(c.source_message_id)
            due = f" due {c.due_at:%Y-%m-%d}" if c.due_at else ""
            when = f" ({c.sent_at:%Y-%m-%d})" if c.sent_at else ""
            lines.append(
                f"[commitment_id={c.id}] {c.kind} {c.status}{due} person={c.person or '?'}: {c.summary}"
                f"{when} [message_id={c.source_message_id}]"
            )
        return "\n".join(lines)

    async def _tool_get_thread(self, thread_id, around_message_id=None) -> str:
        thread, msgs = await get_thread_messages(
            self.session, int(thread_id), int(around_message_id) if around_message_id else None
        )
        if thread is None:
            return "Thread not found."
        self.seen_message_ids.update(m.id for m in msgs)
        lines = [format_message_line(m, m.sender.display_name if m.sender else None) for m in msgs]
        return "\n".join([f"--- {thread.source.kind} thread_id={thread.id} \"{thread.title or ''}\"", *lines])

    async def _tool_find_person(self, name) -> str:
        people = await find_people(self.session, name)
        if not people:
            return f"No person matching {name!r}."
        return "\n".join(
            json.dumps(
                {"id": p.id, "name": p.display_name, "aliases": p.aliases, "emails": p.emails, "telegram": bool(p.telegram_ids)}
            )
            for p in people
        )
