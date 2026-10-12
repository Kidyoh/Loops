"""Split messages into retrievable chunks.

Telegram: consecutive messages in a chat are grouped into windows (~10 messages or
~800 tokens) with a small overlap. Email: one chunk per message, long bodies split by
paragraph. Every chunk keeps the ids of the messages it covers, for citations.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from loops.db.models import Chunk, Message, Source, Thread

WINDOW_MESSAGES = 10
WINDOW_TOKENS = 800
OVERLAP_MESSAGES = 2


def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class Line:
    message_id: int
    sent_at: datetime
    text: str


@dataclass
class ChunkDraft:
    message_ids: list[int]
    text: str
    sent_at: datetime


def format_line(sender: str, sent_at: datetime, body: str) -> str:
    return f"[{sent_at:%Y-%m-%d %H:%M}] {sender}: {body.strip()}"


def window_lines(
    lines: list[Line],
    *,
    context: list[Line] | None = None,
    max_messages: int = WINDOW_MESSAGES,
    max_tokens: int = WINDOW_TOKENS,
    overlap: int = OVERLAP_MESSAGES,
) -> list[ChunkDraft]:
    """Group chat lines into overlapping windows.

    `context` holds already-chunked messages just before `lines`; up to `overlap` of them
    lead the first window so incremental syncs keep conversational context.
    """
    if not lines:
        return []
    lead = (context or [])[-overlap:] if overlap else []
    drafts: list[ChunkDraft] = []
    current: list[Line] = list(lead)
    has_new = False

    def flush() -> None:
        drafts.append(
            ChunkDraft(
                message_ids=[ln.message_id for ln in current],
                text="\n".join(ln.text for ln in current),
                sent_at=current[0].sent_at,
            )
        )

    for line in lines:
        tokens = sum(approx_tokens(ln.text) for ln in current)
        full = len(current) >= max_messages or (current and tokens + approx_tokens(line.text) > max_tokens)
        if full and has_new:
            flush()
            current = current[-overlap:] if overlap else []
            has_new = False
            # Overlap must not crowd out the next message on its own.
            while current and sum(approx_tokens(ln.text) for ln in current) + approx_tokens(line.text) > max_tokens:
                current.pop(0)
        current.append(line)
        has_new = True
    if has_new:
        flush()
    return drafts


def split_paragraphs(text: str, max_tokens: int = WINDOW_TOKENS) -> list[str]:
    """Split a long body on paragraph boundaries into pieces of at most ~max_tokens."""
    if approx_tokens(text) <= max_tokens:
        return [text]
    pieces: list[str] = []
    buf = ""
    for para in [p.strip() for p in text.split("\n\n") if p.strip()]:
        # A single giant paragraph gets hard-split.
        while approx_tokens(para) > max_tokens:
            cut = max_tokens * 4
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(para[:cut])
            para = para[cut:]
        candidate = f"{buf}\n\n{para}" if buf else para
        if approx_tokens(candidate) > max_tokens:
            pieces.append(buf)
            buf = para
        else:
            buf = candidate
    if buf:
        pieces.append(buf)
    return pieces


def _sender_name(m: Message) -> str:
    if m.is_from_me:
        return "Me"
    return m.sender.display_name if m.sender else "Unknown"


async def chunk_pending(session: AsyncSession) -> int:
    """Chunk every message with chunked_at IS NULL. Returns chunks created."""
    pending_threads = (
        await session.execute(
            select(Thread)
            .options(joinedload(Thread.source))
            .where(Thread.id.in_(select(Message.thread_id).where(Message.chunked_at.is_(None))))
        )
    ).scalars().all()

    created = 0
    for thread in pending_threads:
        created += await _chunk_thread(session, thread, thread.source)
    await session.flush()
    return created


async def _chunk_thread(session: AsyncSession, thread: Thread, source: Source) -> int:
    pending = (
        await session.execute(
            select(Message)
            .options(joinedload(Message.sender))
            .where(Message.thread_id == thread.id, Message.chunked_at.is_(None))
            .order_by(Message.sent_at, Message.id)
        )
    ).scalars().all()
    if not pending:
        return 0

    drafts: list[ChunkDraft] = []
    if source.kind == "telegram":
        context_rows = (
            await session.execute(
                select(Message)
                .options(joinedload(Message.sender))
                .where(
                    Message.thread_id == thread.id,
                    Message.chunked_at.is_not(None),
                    Message.sent_at <= pending[0].sent_at,
                )
                .order_by(Message.sent_at.desc(), Message.id.desc())
                .limit(OVERLAP_MESSAGES)
            )
        ).scalars().all()
        to_line = lambda m: Line(m.id, m.sent_at, format_line(_sender_name(m), m.sent_at, m.body_text))  # noqa: E731
        drafts = window_lines(
            [to_line(m) for m in pending if m.body_text.strip()],
            context=[to_line(m) for m in reversed(context_rows)],
        )
    else:
        for m in pending:
            if not m.body_text.strip():
                continue
            header = f"Subject: {thread.title or '(no subject)'}\nFrom: {_sender_name(m)}\nDate: {m.sent_at:%Y-%m-%d %H:%M}\n\n"
            for piece in split_paragraphs(m.body_text):
                drafts.append(ChunkDraft(message_ids=[m.id], text=header + piece, sent_at=m.sent_at))

    for d in drafts:
        session.add(
            Chunk(
                message_id=d.message_ids[0],
                message_ids=d.message_ids,
                thread_id=thread.id,
                text=d.text,
                sent_at=d.sent_at,
            )
        )
    now = datetime.now(UTC)
    await session.execute(
        update(Message).where(Message.id.in_([m.id for m in pending])).values(chunked_at=now)
    )
    return len(drafts)
