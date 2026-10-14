"""Load (or remove) the demo dataset through the normal ingest path."""

import logging

from sqlalchemy import delete, or_, select, text

from loops.db.models import Person, Source
from loops.db.session import session_scope
from loops.demo.data import DEMO_EMAIL_SUFFIX, DEMO_TELEGRAM_IDS, GMAIL_ACCOUNT, TELEGRAM_ACCOUNT, build_messages
from loops.ingest.normalize import get_or_create_source, merge_people, store_messages, suggest_merges
from loops.ingest.pipeline import process_pending

log = logging.getLogger(__name__)

DEMO_ACCOUNTS = {("telegram", TELEGRAM_ACCOUNT), ("gmail", GMAIL_ACCOUNT)}


class DemoError(RuntimeError):
    pass


async def reset_demo() -> int:
    """Delete demo sources (and everything derived) plus demo people. Returns sources removed."""
    async with session_scope() as session:
        sources = (await session.execute(select(Source))).scalars().all()
        demo = [s for s in sources if (s.kind, s.account) in DEMO_ACCOUNTS]
        for s in demo:
            await session.delete(s)
        await session.flush()
        email_match = text(
            "EXISTS (SELECT 1 FROM unnest(people.emails) e WHERE e LIKE :suffix)"
        ).bindparams(suffix=f"%{DEMO_EMAIL_SUFFIX}")
        await session.execute(
            delete(Person).where(or_(email_match, Person.telegram_ids.overlap(sorted(DEMO_TELEGRAM_IDS))))
        )
    return len(demo)


async def seed_demo(*, force: bool = False, process: bool = True, llm=None) -> dict:
    """Seed the demo data. Refuses to mix with real sources unless force=True."""
    async with session_scope() as session:
        sources = (await session.execute(select(Source))).scalars().all()
        real = [s for s in sources if (s.kind, s.account) not in DEMO_ACCOUNTS]
        if real and not force:
            raise DemoError(
                f"database already has real sources ({', '.join(s.account for s in real)}); "
                "use a separate database for the demo, or pass --force"
            )
    await reset_demo()

    stored = 0
    by_source = build_messages()
    async with session_scope() as session:
        for kind, account in (("telegram", TELEGRAM_ACCOUNT), ("gmail", GMAIL_ACCOUNT)):
            source = await get_or_create_source(session, kind, account)
            stored += await store_messages(session, source, by_source[kind])
        # Robi appears on Telegram and email: merge him like a user would after review.
        merged = 0
        for mail_person, tg_person in await suggest_merges(session):
            await merge_people(session, mail_person.id, tg_person.id)
            merged += 1

    stats = {"messages": stored, "people_merged": merged}
    if process:
        stats.update(await process_pending(llm))
    return stats
