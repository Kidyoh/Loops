"""Scheduler: incremental sync every SYNC_INTERVAL_MIN minutes, digest daily at DIGEST_HOUR."""

import asyncio
import logging
from datetime import UTC, datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from loops.bot.digest import send_digest
from loops.core.config import get_settings
from loops.worker.jobs import sync_all

log = logging.getLogger(__name__)


async def _sync_job() -> None:
    try:
        log.info("sync: %s", await sync_all())
    except Exception:
        log.exception("sync job failed")


async def _digest_job() -> None:
    try:
        log.info("digest sent: %s", await send_digest())
    except Exception:
        log.exception("digest job failed")


def build_scheduler() -> AsyncIOScheduler:
    s = get_settings()
    scheduler = AsyncIOScheduler(timezone=s.timezone)
    scheduler.add_job(
        _sync_job,
        IntervalTrigger(minutes=s.sync_interval_min),
        id="sync",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(UTC),
    )
    scheduler.add_job(
        _digest_job,
        CronTrigger(hour=s.digest_hour, minute=0, timezone=s.timezone),
        id="digest",
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    return scheduler


async def run_worker() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheduler = build_scheduler()
    scheduler.start()
    log.info("worker started: sync every %s min, digest at %02d:00 %s",
             get_settings().sync_interval_min, get_settings().digest_hour, get_settings().timezone)
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown(wait=False)


if __name__ == "__main__":
    asyncio.run(run_worker())
