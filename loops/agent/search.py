"""Hybrid retrieval: pgvector cosine + Postgres full-text, merged with reciprocal rank fusion."""

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import Select, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from loops.core.config import get_settings
from loops.db.models import TS_CONFIG, Chunk, Message, Person, Source, Thread
from loops.db.vector import cosine_distance_sql, to_pgvector_literal

RRF_K = 60


@dataclass
class Hit:
    chunk_id: int
    thread_id: int
    thread_title: str | None
    source: str
    sent_at: datetime
    message_ids: list[int]
    score: float = 0.0
    # Message lines rendered for the model, each tagged with its message id.
    lines: list[str] = field(default_factory=list)


async def find_people(session: AsyncSession, name: str, limit: int = 5) -> list[Person]:
    """Match a name/handle/email against display names, aliases and emails."""
    q = name.strip().lstrip("@").lower()
    if not q:
        return []
    pattern = f"%{q}%"
    alias_match = text("EXISTS (SELECT 1 FROM unnest(people.aliases) a WHERE lower(a) LIKE :p)").bindparams(p=pattern)
    email_match = text("EXISTS (SELECT 1 FROM unnest(people.emails) e WHERE e LIKE :p2)").bindparams(p2=pattern)
    rows = (
        await session.execute(
            select(Person)
            .where(or_(func.lower(Person.display_name).like(pattern), alias_match, email_match))
            .order_by(func.length(Person.display_name))
            .limit(limit)
        )
    ).scalars().all()
    return list(rows)


def _filters(
    stmt: Select,
    *,
    person_ids: list[int] | None,
    source: str | None,
    after: datetime | None,
    before: datetime | None,
) -> Select:
    stmt = stmt.join(Thread, Thread.id == Chunk.thread_id).join(Source, Source.id == Thread.source_id)
    conds = []
    if person_ids:
        conds.append(or_(*(Thread.participants.contains([pid]) for pid in person_ids)))
    if source:
        conds.append(Source.kind == source)
    if after:
        conds.append(Chunk.sent_at >= after)
    if before:
        conds.append(Chunk.sent_at < before)
    return stmt.where(and_(*conds)) if conds else stmt


async def hybrid_search(
    session: AsyncSession,
    llm,
    query: str,
    *,
    person_ids: list[int] | None = None,
    source: str | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    k: int = 8,
) -> list[Hit]:
    pool = max(k * 4, 20)
    filters = dict(person_ids=person_ids, source=source, after=after, before=before)
    ranked: dict[int, float] = {}

    if query.strip():
        qvec = (await llm.embed([query]))[0]
        dist = cosine_distance_sql(get_settings().embed_dim)
        vec_stmt = _filters(select(Chunk.id).where(Chunk.embedding.is_not(None)), **filters)
        vec_stmt = vec_stmt.order_by(dist).limit(pool)
        vec_ids = (await session.execute(vec_stmt, {"qvec": to_pgvector_literal(qvec)})).scalars().all()
        for rank, cid in enumerate(vec_ids):
            ranked[cid] = ranked.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)

        tsq = func.websearch_to_tsquery(TS_CONFIG, query)
        fts_stmt = _filters(select(Chunk.id).where(Chunk.tsv.op("@@")(tsq)), **filters)
        fts_stmt = fts_stmt.order_by(func.ts_rank_cd(Chunk.tsv, tsq).desc()).limit(pool)
        for rank, cid in enumerate((await session.execute(fts_stmt)).scalars().all()):
            ranked[cid] = ranked.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    else:
        # No query text: most recent chunks matching the filters.
        recent = _filters(select(Chunk.id), **filters).order_by(Chunk.sent_at.desc()).limit(k)
        for rank, cid in enumerate((await session.execute(recent)).scalars().all()):
            ranked[cid] = 1.0 / (RRF_K + rank + 1)

    top = sorted(ranked.items(), key=lambda kv: kv[1], reverse=True)[:k]
    if not top:
        return []
    rows = (
        await session.execute(
            select(Chunk, Thread.title, Source.kind)
            .join(Thread, Thread.id == Chunk.thread_id)
            .join(Source, Source.id == Thread.source_id)
            .where(Chunk.id.in_([cid for cid, _ in top]))
        )
    ).all()
    by_id = {c.id: (c, title, kind) for c, title, kind in rows}
    hits = []
    for cid, score in top:
        c, title, kind = by_id[cid]
        hits.append(Hit(cid, c.thread_id, title, kind, c.sent_at, list(c.message_ids), score))
    await _render_lines(session, hits)
    return hits


async def _render_lines(session: AsyncSession, hits: list[Hit]) -> None:
    ids = {mid for h in hits for mid in h.message_ids}
    if not ids:
        return
    msgs = (
        await session.execute(
            select(Message, Person.display_name)
            .outerjoin(Person, Person.id == Message.sender_id)
            .where(Message.id.in_(ids))
        )
    ).all()
    by_id = {m.id: (m, name) for m, name in msgs}
    for h in hits:
        for mid in h.message_ids:
            if mid in by_id:
                m, name = by_id[mid]
                h.lines.append(format_message_line(m, name))


def format_message_line(m: Message, sender_name: str | None, max_chars: int = 1500) -> str:
    who = "Me" if m.is_from_me else (sender_name or "Unknown")
    body = m.body_text if len(m.body_text) <= max_chars else m.body_text[:max_chars] + " …"
    return f"[message_id={m.id}] {m.sent_at:%Y-%m-%d %H:%M} {who}: {body}"
