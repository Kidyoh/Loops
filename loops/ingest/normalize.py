"""Turn IncomingMessages into threads, people and messages rows."""

import re
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from loops.db.models import Message, Person, Source, Thread
from loops.ingest.types import IncomingMessage, SenderRef

# --- email cleanup -----------------------------------------------------------------

_REPLY_HEADERS = [
    re.compile(r"^On .{0,200}wrote:\s*$", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Original Message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^-{2,}\s*Forwarded message\s*-{2,}", re.IGNORECASE),
    re.compile(r"^From:\s.+", re.IGNORECASE),
    re.compile(r"^_{5,}\s*$"),
]
_SIGNATURE_MARKERS = [
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my (iPhone|iPad|Android|phone|mobile)", re.IGNORECASE),
    re.compile(r"^Get Outlook for", re.IGNORECASE),
]


def clean_email_body(text: str) -> str:
    """Keep only the new text of an email: drop quoted replies and signatures."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if any(p.match(stripped) for p in _REPLY_HEADERS + _SIGNATURE_MARKERS):
            break
        # Gmail sometimes wraps "On ... wrote:" across two lines.
        if stripped.startswith("On ") and i + 1 < len(lines) and lines[i + 1].strip().endswith("wrote:"):
            break
        if stripped.startswith(">"):
            continue
        out.append(line.rstrip())
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# --- people ------------------------------------------------------------------------


def _norm_email(email: str | None) -> str | None:
    return email.strip().lower() if email else None


async def get_me(session: AsyncSession) -> Person:
    me = (await session.execute(select(Person).where(Person.is_me))).scalars().first()
    if me is None:
        me = Person(display_name="Me", is_me=True, emails=[], telegram_ids=[], aliases=[])
        session.add(me)
        await session.flush()
    return me


async def resolve_person(session: AsyncSession, ref: SenderRef, *, is_me: bool = False) -> Person:
    """Find or create a person, merging identities by email and Telegram id."""
    email = _norm_email(ref.email)
    person: Person | None = None
    if is_me:
        person = await get_me(session)
    if person is None and email:
        person = (
            await session.execute(select(Person).where(Person.emails.contains([email])))
        ).scalars().first()
    if person is None and ref.telegram_id is not None:
        person = (
            await session.execute(select(Person).where(Person.telegram_ids.contains([ref.telegram_id])))
        ).scalars().first()

    name = (ref.name or "").strip() or email or (str(ref.telegram_id) if ref.telegram_id else "Unknown")
    if person is None:
        person = Person(display_name=name, emails=[], telegram_ids=[], aliases=[])
        session.add(person)

    if email and email not in (person.emails or []):
        person.emails = [*(person.emails or []), email]
    if ref.telegram_id is not None and ref.telegram_id not in (person.telegram_ids or []):
        person.telegram_ids = [*(person.telegram_ids or []), ref.telegram_id]
    if name != person.display_name and not _is_identifier(name):
        if _is_identifier(person.display_name):
            # A real name beats a bare email, numeric id or placeholder.
            person.display_name = name
        elif name not in (person.aliases or []):
            person.aliases = [*(person.aliases or []), name]
    await session.flush()
    return person


def _is_identifier(name: str) -> bool:
    return "@" in name or name.isdigit() or name in {"Me", "Unknown"}


async def merge_people(session: AsyncSession, keep_id: int, merge_id: int) -> Person:
    """Manually merge two people (e.g. same person on Gmail and Telegram)."""
    from sqlalchemy import update

    from loops.db.models import Commitment

    keep = await session.get(Person, keep_id)
    other = await session.get(Person, merge_id)
    if keep is None or other is None or keep_id == merge_id:
        raise ValueError("both people must exist and differ")
    keep.emails = sorted(set(keep.emails) | set(other.emails))
    keep.telegram_ids = sorted(set(keep.telegram_ids) | set(other.telegram_ids))
    keep.aliases = sorted((set(keep.aliases) | set(other.aliases) | {other.display_name}) - {keep.display_name})
    keep.is_me = keep.is_me or other.is_me
    await session.execute(update(Message).where(Message.sender_id == merge_id).values(sender_id=keep_id))
    await session.execute(update(Commitment).where(Commitment.person_id == merge_id).values(person_id=keep_id))
    await session.execute(
        update(Thread)
        .where(Thread.participants.contains([merge_id]))
        .values(
            participants=func.array_append(
                func.array_remove(func.array_remove(Thread.participants, merge_id), keep_id), keep_id
            )
        )
    )
    await session.delete(other)
    await session.flush()
    return keep


# --- messages ----------------------------------------------------------------------


async def get_or_create_source(session: AsyncSession, kind: str, account: str) -> Source:
    src = (
        await session.execute(select(Source).where(Source.kind == kind, Source.account == account))
    ).scalars().first()
    if src is None:
        src = Source(kind=kind, account=account)
        session.add(src)
        await session.flush()
    return src


async def store_messages(session: AsyncSession, source: Source, items: list[IncomingMessage]) -> int:
    """Upsert threads/people and insert new messages. Returns the number of new messages."""
    if not items:
        return 0
    by_thread: dict[str, list[IncomingMessage]] = defaultdict(list)
    for item in items:
        by_thread[item.thread_external_id].append(item)

    inserted = 0
    for ext_id, msgs in by_thread.items():
        thread = (
            await session.execute(
                select(Thread).where(Thread.source_id == source.id, Thread.external_id == ext_id)
            )
        ).scalars().first()
        if thread is None:
            thread = Thread(source_id=source.id, external_id=ext_id, participants=[])
            session.add(thread)
            await session.flush()
        title = next((m.thread_title for m in msgs if m.thread_title), None)
        if title and not thread.title:
            thread.title = title

        participant_ids = set(thread.participants or [])
        for m in sorted(msgs, key=lambda m: m.sent_at):
            sender = await resolve_person(session, m.sender, is_me=m.is_from_me)
            participant_ids.add(sender.id)
            for ref in m.participants:
                participant_ids.add((await resolve_person(session, ref)).id)
            stmt = (
                insert(Message)
                .values(
                    thread_id=thread.id,
                    external_id=m.external_id,
                    sender_id=sender.id,
                    is_from_me=m.is_from_me,
                    sent_at=m.sent_at,
                    body_text=m.body_text,
                    url=m.url,
                    raw=m.raw,
                )
                .on_conflict_do_nothing(index_elements=["thread_id", "external_id"])
                .returning(Message.id)
            )
            if (await session.execute(stmt)).scalar() is not None:
                inserted += 1
            if thread.last_message_at is None or m.sent_at > thread.last_message_at:
                thread.last_message_at = m.sent_at
        thread.participants = sorted(participant_ids)
    await session.flush()
    return inserted


def _name_key(name: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", name.lower()).split())


async def suggest_merges(session: AsyncSession) -> list[tuple[Person, Person]]:
    """Pairs of (email-only person, telegram-only person) sharing a name or alias.

    Cross-source identities can't be linked by id, so this is a review list rather than
    an automatic merge. Single-word names are skipped as too ambiguous.
    """
    people = (await session.execute(select(Person).where(Person.is_me.is_(False)))).scalars().all()
    by_name: dict[str, list[Person]] = defaultdict(list)
    for p in people:
        for name in {p.display_name, *(p.aliases or [])}:
            key = _name_key(name)
            if len(key.split()) >= 2:
                by_name[key].append(p)
    pairs: dict[tuple[int, int], tuple[Person, Person]] = {}
    for group in by_name.values():
        mail = [p for p in group if p.emails and not p.telegram_ids]
        tg = [p for p in group if p.telegram_ids and not p.emails]
        if len(mail) == 1 and len(tg) == 1:
            pairs[(mail[0].id, tg[0].id)] = (mail[0], tg[0])
    return list(pairs.values())
