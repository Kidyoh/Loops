"""Jobs shared by the scheduler, the API, the bot and the CLI."""

import logging
from pathlib import Path

from sqlalchemy import text

from loops.core.config import get_settings
from loops.db.session import get_engine
from loops.ingest.pipeline import process_pending

log = logging.getLogger(__name__)

# Postgres advisory lock id: one sync at a time across the worker, API and bot processes.
SYNC_LOCK_ID = 7_331_001


async def sync_all() -> dict:
    """Incremental sync of every connected source, then chunk, embed and extract.

    Returns {"skipped": ...} if another process is already syncing.
    """
    async with get_engine().connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:id)"), {"id": SYNC_LOCK_ID})).scalar()
        if not got:
            return {"skipped": "sync already running"}
        try:
            return await _sync_sources()
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": SYNC_LOCK_ID})
            await lock_conn.commit()


async def _sync_sources() -> dict:
    stats: dict = {}
    if Path(get_settings().tg_session_path).exists():
        from loops.ingest.telegram import sync_telegram

        try:
            stats["telegram"] = await sync_telegram()
        except Exception:
            log.exception("telegram sync failed")
            stats["telegram"] = "error"
    from loops.ingest.gmail import sync_gmail

    try:
        stats["gmail"] = await sync_gmail()
    except Exception:
        log.exception("gmail sync failed")
        stats["gmail"] = "error"
    stats.update(await process_pending())
    return stats
