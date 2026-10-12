"""Post-ingest processing: chunk new messages, then embed new chunks."""

import logging

from loops.db.session import session_scope
from loops.ingest.chunk import chunk_pending
from loops.ingest.embed import embed_pending
from loops.llm.token_factory import TokenFactoryClient, get_llm

log = logging.getLogger(__name__)


async def process_pending(llm: TokenFactoryClient | None = None) -> dict:
    llm = llm or get_llm()
    async with session_scope() as session:
        chunks = await chunk_pending(session)
    async with session_scope() as session:
        embedded = await embed_pending(session, llm)
    stats = {"chunks": chunks, "embedded": embedded}
    log.info("pipeline: %s", stats)
    return stats
