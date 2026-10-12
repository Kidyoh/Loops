from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loops.db.models import SyncState


async def get_state(session: AsyncSession, source_id: int, key: str) -> SyncState | None:
    return (
        await session.execute(select(SyncState).where(SyncState.source_id == source_id, SyncState.key == key))
    ).scalars().first()


async def set_state(session: AsyncSession, source_id: int, key: str, cursor: str | None) -> None:
    state = await get_state(session, source_id, key)
    if state is None:
        state = SyncState(source_id=source_id, key=key)
        session.add(state)
    state.cursor = cursor
    state.last_synced_at = datetime.now(UTC)
