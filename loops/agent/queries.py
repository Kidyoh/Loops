"""Structured reads shared by the agent tools, the API and the bot."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from loops.db.models import Commitment, Message, Person, Source, Thread


@dataclass
class CommitmentView:
    id: int
    kind: str
    status: str
    summary: str
    person: str | None
    person_id: int | None
    due_at: datetime | None
    confidence: float
    source_message_id: int | None
    source: str | None
    url: str | None
    sent_at: datetime | None
    created_at: datetime

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        for key in ("due_at", "sent_at", "created_at"):
            d[key] = d[key].isoformat() if d[key] else None
        return d


async def list_commitments(
    session: AsyncSession,
    *,
    kind: str | None = None,
    status: str | None = "open",
    person_ids: list[int] | None = None,
    after: datetime | None = None,
    before: datetime | None = None,
    limit: int = 100,
) -> list[CommitmentView]:
    stmt = (
        select(Commitment, Person.display_name, Message, Source.kind)
        .outerjoin(Person, Person.id == Commitment.person_id)
        .outerjoin(Message, Message.id == Commitment.source_message_id)
        .outerjoin(Thread, Thread.id == Message.thread_id)
        .outerjoin(Source, Source.id == Thread.source_id)
    )
    if kind:
        stmt = stmt.where(Commitment.kind == kind)
    if status:
        stmt = stmt.where(Commitment.status == status)
    if person_ids:
        stmt = stmt.where(Commitment.person_id.in_(person_ids))
    # Date filters apply to when the commitment was made (its evidence message).
    if after:
        stmt = stmt.where(Message.sent_at >= after)
    if before:
        stmt = stmt.where(Message.sent_at < before)
    stmt = stmt.order_by(Commitment.due_at.asc().nulls_last(), Message.sent_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).all()
    return [
        CommitmentView(
            id=c.id,
            kind=c.kind,
            status=c.status,
            summary=c.summary,
            person=name,
            person_id=c.person_id,
            due_at=c.due_at,
            confidence=c.confidence,
            source_message_id=c.source_message_id,
            source=src,
            url=m.url if m else None,
            sent_at=m.sent_at if m else None,
            created_at=c.created_at,
        )
        for c, name, m, src in rows
    ]


async def get_thread_messages(
    session: AsyncSession, thread_id: int, around_message_id: int | None = None, window: int = 10
) -> tuple[Thread | None, list[Message]]:
    thread = (
        await session.execute(select(Thread).options(joinedload(Thread.source)).where(Thread.id == thread_id))
    ).scalars().first()
    if thread is None:
        return None, []
    base = select(Message).options(joinedload(Message.sender)).where(Message.thread_id == thread_id)
    if around_message_id is None:
        rows = (await session.execute(base.order_by(Message.sent_at.desc()).limit(window * 2))).scalars().all()
        return thread, list(reversed(rows))
    pivot = await session.get(Message, around_message_id)
    if pivot is None or pivot.thread_id != thread_id:
        return thread, []
    before = (
        await session.execute(
            base.where(or_(Message.sent_at < pivot.sent_at, (Message.sent_at == pivot.sent_at) & (Message.id < pivot.id)))
            .order_by(Message.sent_at.desc(), Message.id.desc())
            .limit(window)
        )
    ).scalars().all()
    after = (
        await session.execute(
            base.where(or_(Message.sent_at > pivot.sent_at, (Message.sent_at == pivot.sent_at) & (Message.id >= pivot.id)))
            .order_by(Message.sent_at, Message.id)
            .limit(window + 1)
        )
    ).scalars().all()
    return thread, [*reversed(before), *after]
