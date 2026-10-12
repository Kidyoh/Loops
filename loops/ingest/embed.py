from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from loops.db.models import Chunk
from loops.llm.token_factory import TokenFactoryClient

BATCH = 256


async def embed_pending(session: AsyncSession, llm: TokenFactoryClient) -> int:
    """Embed every chunk that has no embedding yet. Commits after each batch."""
    total = 0
    while True:
        chunks = (
            await session.execute(
                select(Chunk).where(Chunk.embedding.is_(None)).order_by(Chunk.id).limit(BATCH)
            )
        ).scalars().all()
        if not chunks:
            return total
        vectors = await llm.embed([c.text for c in chunks])
        for chunk, vec in zip(chunks, vectors, strict=True):
            chunk.embedding = vec
        await session.commit()
        total += len(chunks)
