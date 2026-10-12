"""Commitment extraction (Nemotron, strict JSON) plus a non-LLM waiting_on rule.

Runs per thread, only on messages with extracted_at IS NULL. Prior open items for the
thread go into the prompt so the model can mark them done when a new message resolves them.
"""

import logging
import re
from datetime import UTC, datetime, time, timedelta
from importlib.resources import files
from string import Template
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from loops.agent.search import format_message_line
from loops.core.config import get_settings
from loops.db.models import COMMITMENT_KINDS, Commitment, Message, Person, Thread
from loops.llm.token_factory import LLMError

log = logging.getLogger(__name__)

CONTEXT_MESSAGES = 8
BATCH_MESSAGES = 30
MAX_PROMPT_CHARS = 24000

PROMPT = Template(files("loops.extract").joinpath("prompts/commitments.txt").read_text())

REQUEST_RE = re.compile(
    r"\?|\b(can|could|would) you\b|\bplease\b|\blet me know\b|\bany updates?\b|\bwaiting (on|for)\b|"
    r"\bsend (me|over)\b|\bget back to me\b|\bremind me\b",
    re.IGNORECASE,
)


def end_of_day(day: str, tz: str) -> datetime | None:
    try:
        d = datetime.strptime(day.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return None
    return datetime.combine(d, time(23, 59, 59), ZoneInfo(tz)).astimezone(UTC)


def _match_person(name: str | None, candidates: list[Person]) -> int | None:
    """Resolve the model's person string against the thread's participants."""
    key = (name or "").strip().lstrip("@").lower()
    if not key:
        return None
    for p in candidates:
        names = [p.display_name, *(p.aliases or []), *(p.emails or [])]
        if any(key == n.lower() for n in names):
            return p.id
    for p in candidates:
        names = [p.display_name, *(p.aliases or [])]
        if any(key in n.lower() or n.lower().split()[0] == key.split()[0] for n in names if n):
            return p.id
    return None


async def extract_pending(session: AsyncSession, llm) -> dict:
    """Extract commitments from every thread with unprocessed messages."""
    thread_ids = (
        await session.execute(select(Message.thread_id).where(Message.extracted_at.is_(None)).distinct())
    ).scalars().all()
    stats = {"threads": 0, "created": 0, "resolved": 0, "failed": 0}
    for tid in thread_ids:
        try:
            created, resolved = await extract_thread(session, llm, tid)
        except LLMError as e:
            # Leave extracted_at NULL so the next run retries this thread.
            log.warning("extraction failed for thread %s: %s", tid, e)
            stats["failed"] += 1
            await session.rollback()
            continue
        await session.commit()
        stats["threads"] += 1
        stats["created"] += created
        stats["resolved"] += resolved
    return stats


async def extract_thread(session: AsyncSession, llm, thread_id: int) -> tuple[int, int]:
    s = get_settings()
    thread = (
        await session.execute(select(Thread).options(joinedload(Thread.source)).where(Thread.id == thread_id))
    ).scalars().one()
    pending = (
        await session.execute(
            select(Message)
            .options(joinedload(Message.sender))
            .where(Message.thread_id == thread_id, Message.extracted_at.is_(None))
            .order_by(Message.sent_at, Message.id)
        )
    ).scalars().all()
    participants = (
        await session.execute(select(Person).where(Person.id.in_(thread.participants or [])))
    ).scalars().all()

    created = resolved = 0
    for i in range(0, len(pending), BATCH_MESSAGES):
        batch = pending[i : i + BATCH_MESSAGES]
        c, r = await _extract_batch(session, llm, thread, batch, list(participants), s)
        created += c
        resolved += r
        await session.execute(
            update(Message).where(Message.id.in_([m.id for m in batch])).values(extracted_at=datetime.now(UTC))
        )
    return created, resolved


async def _extract_batch(session, llm, thread: Thread, batch: list[Message], participants: list[Person], s) -> tuple[int, int]:
    batch = [m for m in batch if m.body_text.strip()]
    if not batch:
        return 0, 0
    context = (
        await session.execute(
            select(Message)
            .options(joinedload(Message.sender))
            .where(Message.thread_id == thread.id, Message.extracted_at.is_not(None), Message.sent_at <= batch[0].sent_at)
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(CONTEXT_MESSAGES)
        )
    ).scalars().all()
    open_items = (
        await session.execute(
            select(Commitment).where(Commitment.thread_id == thread.id, Commitment.status == "open")
        )
    ).scalars().all()

    lines = [format_message_line(m, m.sender.display_name if m.sender else None) for m in reversed(context)]
    lines += ["NEW " + format_message_line(m, m.sender.display_name if m.sender else None) for m in batch]
    text = "\n".join(lines)
    if len(text) > MAX_PROMPT_CHARS:
        text = text[-MAX_PROMPT_CHARS:]
    prompt = PROMPT.substitute(
        today=datetime.now(ZoneInfo(s.timezone)).date().isoformat(),
        tz=s.timezone,
        source=thread.source.kind,
        title=thread.title or "",
        open_items="\n".join(f"[id={c.id}] {c.kind}: {c.summary}" for c in open_items) or "(none)",
        messages=text,
    )
    data = await llm.chat_json([{"role": "user", "content": prompt}], max_tokens=2000)
    if not isinstance(data, dict):
        raise LLMError("extraction output is not a JSON object")

    by_id = {m.id: m for m in batch}
    created = 0
    for item in data.get("commitments") or []:
        if not isinstance(item, dict):
            continue
        commitment = await _validate(session, item, by_id, participants, s)
        if commitment is not None:
            session.add(commitment)
            created += 1

    visible = {m.id for m in batch} | {m.id for m in context}
    open_by_id = {c.id: c for c in open_items}
    resolved = 0
    for item in data.get("resolved") or []:
        if not isinstance(item, dict):
            continue
        c = open_by_id.get(_as_int(item.get("id")))
        if c is not None and _as_int(item.get("evidence_message_id")) in visible:
            c.status = "done"
            resolved += 1
    await session.flush()
    return created, resolved


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def _validate(session, item: dict, batch_by_id: dict[int, Message], participants: list[Person], s) -> Commitment | None:
    kind = item.get("kind")
    summary = (item.get("summary") or "").strip()
    try:
        confidence = float(item.get("confidence") or 0)
    except (TypeError, ValueError):
        return None
    evidence = batch_by_id.get(_as_int(item.get("evidence_message_id")))
    if kind not in COMMITMENT_KINDS or not summary or evidence is None or confidence < s.extract_min_confidence:
        return None
    if kind in ("i_promised", "waiting_on") and not evidence.is_from_me:
        return None
    if kind == "they_promised" and evidence.is_from_me:
        return None
    exists = (
        await session.execute(
            select(Commitment.id).where(Commitment.source_message_id == evidence.id, Commitment.kind == kind)
        )
    ).first()
    if exists:
        return None

    others = [p for p in participants if not p.is_me]
    person_id = _match_person(item.get("person"), others)
    if person_id is None and kind == "they_promised" and evidence.sender_id:
        person_id = evidence.sender_id
    if person_id is None and len(others) == 1:
        person_id = others[0].id
    return Commitment(
        kind=kind,
        person_id=person_id,
        thread_id=evidence.thread_id,
        summary=summary[:500],
        due_at=end_of_day(item.get("due") or "", s.timezone),
        status="open",
        confidence=confidence,
        origin="llm",
        source_message_id=evidence.id,
    )


async def apply_waiting_on_rule(session: AsyncSession, now: datetime | None = None) -> dict:
    """No-LLM waiting_on: the last message in a thread is mine, asks for something,
    and nobody has replied in WAITING_ON_DAYS. Rule items close when someone replies."""
    s = get_settings()
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=s.waiting_on_days)

    # Close rule-based items once someone else has written after them.
    open_rule = (
        await session.execute(
            select(Commitment, Message)
            .join(Message, Message.id == Commitment.source_message_id)
            .where(Commitment.origin == "rule", Commitment.status == "open")
        )
    ).all()
    closed = 0
    for c, src in open_rule:
        reply = (
            await session.execute(
                select(Message.id).where(
                    Message.thread_id == src.thread_id, Message.sent_at > src.sent_at, Message.is_from_me.is_(False)
                ).limit(1)
            )
        ).first()
        if reply:
            c.status = "done"
            closed += 1

    last_per_thread = (
        select(Message.id)
        .ext(distinct_on(Message.thread_id))
        .order_by(Message.thread_id, Message.sent_at.desc(), Message.id.desc())
        .subquery()
    )
    candidates = (
        await session.execute(
            select(Message, Thread)
            .join(Thread, Thread.id == Message.thread_id)
            .where(Message.id.in_(select(last_per_thread.c.id)), Message.is_from_me.is_(True), Message.sent_at < cutoff)
            .where(Message.sent_at > now - timedelta(days=s.backfill_days))
        )
    ).all()
    created = 0
    for m, thread in candidates:
        if not REQUEST_RE.search(m.body_text):
            continue
        exists = (
            await session.execute(
                select(Commitment.id).where(
                    Commitment.kind == "waiting_on",
                    Commitment.status == "open",
                    (Commitment.source_message_id == m.id)
                    | ((Commitment.thread_id == thread.id) & (Commitment.origin == "rule")),
                )
            )
        ).first()
        if exists:
            continue  # already tracked, or a follow-up to an item still waiting
        others = [pid for pid in (thread.participants or []) if pid != m.sender_id]
        snippet = " ".join(m.body_text.split())
        snippet = snippet if len(snippet) <= 140 else snippet[:137] + "..."
        session.add(
            Commitment(
                kind="waiting_on",
                person_id=others[0] if len(others) == 1 else None,
                thread_id=thread.id,
                summary=f"No reply yet: \"{snippet}\"",
                status="open",
                confidence=0.7,
                origin="rule",
                source_message_id=m.id,
            )
        )
        created += 1
    await session.flush()
    return {"rule_created": created, "rule_closed": closed}
