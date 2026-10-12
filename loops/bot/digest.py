"""Daily digest: overdue promises, what's due today, and who you're waiting on."""

import logging
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from loops.agent.queries import list_commitments
from loops.bot.format import format_digest
from loops.core.config import get_settings
from loops.db.session import session_scope

log = logging.getLogger(__name__)


async def build_digest(session: AsyncSession, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    tz = ZoneInfo(get_settings().timezone)
    local_today = now.astimezone(tz).date()
    end_of_today = datetime.combine(local_today + timedelta(days=1), time.min, tz)

    promises = await list_commitments(session, kind="i_promised", status="open")
    overdue = [c for c in promises if c.due_at and c.due_at < now]
    due_today = [
        c
        for c in [*promises, *await list_commitments(session, kind="they_promised", status="open")]
        if c.due_at and now <= c.due_at < end_of_today
    ]
    waiting = (await list_commitments(session, kind="waiting_on", status="open"))[:15]
    return format_digest(overdue, due_today, waiting, now)


async def send_digest(bot=None) -> bool:
    """Send the digest to the owner. Returns False when there was nothing to send."""
    s = get_settings()
    if not s.bot_token or not s.owner_telegram_id:
        log.warning("digest skipped: BOT_TOKEN or OWNER_TELEGRAM_ID not set")
        return False
    async with session_scope() as session:
        text = await build_digest(session)
    if text is None:
        return False
    if bot is None:
        from telegram import Bot

        bot = Bot(s.bot_token)
    async with bot:
        await bot.send_message(
            chat_id=s.owner_telegram_id, text=text, parse_mode="HTML", disable_web_page_preview=True
        )
    return True
