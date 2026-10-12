"""Jobs shared by the scheduler, the API and the CLI."""

import asyncio
import logging
from pathlib import Path

from loops.core.config import get_settings
from loops.ingest.pipeline import process_pending

log = logging.getLogger(__name__)

_sync_lock = asyncio.Lock()


async def sync_all() -> dict:
    """Incremental sync of every connected source, then chunk + embed (+ extract).

    Runs one at a time; a call made while a sync is running waits for it.
    """
    async with _sync_lock:
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
