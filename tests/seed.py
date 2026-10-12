"""Seed a small cross-source dataset through the real ingest path."""

from sqlalchemy import select

from loops.db.models import Message
from loops.ingest.normalize import get_or_create_source, store_messages
from loops.ingest.pipeline import process_pending
from loops.ingest.types import IncomingMessage, SenderRef
from tests.fakes import FakeLLM, dt

ROBI_TG = SenderRef("Robi Tesfaye", telegram_id=2)
ME_TG = SenderRef("Abebe", telegram_id=1)
SARA = SenderRef("Sara Bekele", email="sara@x.com")
ME_MAIL = SenderRef("Abebe", email="abebe@example.com")


async def seed(session) -> dict[str, int]:
    tg = await get_or_create_source(session, "telegram", "1")
    gm = await get_or_create_source(session, "gmail", "abebe@example.com")
    await store_messages(
        session,
        tg,
        [
            IncomingMessage("2", "1", ROBI_TG, False, dt(1, 9), "What did you think of the Habesha pitch deck?", "Robi Tesfaye"),
            IncomingMessage("2", "2", ME_TG, True, dt(1, 10), "Looks strong. I'll send detailed comments tomorrow.", "Robi Tesfaye"),
            IncomingMessage("2", "3", ROBI_TG, False, dt(2, 9), "Let's move the investor call to next week", "Robi Tesfaye"),
        ],
    )
    await store_messages(
        session,
        gm,
        [
            IncomingMessage("t1", "g1", ME_MAIL, True, dt(3, 8), "Hi Sara, could you send the signed contract?", "Contract", participants=[SARA]),
            IncomingMessage("t2", "g2", SARA, False, dt(4, 8), "Quarterly budget numbers attached.", "Budget"),
        ],
    )
    await session.commit()
    await process_pending(FakeLLM(), extract=False)
    rows = (await session.execute(select(Message.external_id, Message.id))).all()
    return {ext: mid for ext, mid in rows}
