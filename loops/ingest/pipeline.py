"""Post-ingest processing: chunk and embed new messages, then extract commitments."""

import logging

from loops.db.session import session_scope
from loops.extract.commitments import apply_waiting_on_rule, extract_pending
from loops.ingest.chunk import chunk_pending
from loops.ingest.embed import embed_pending
from loops.llm.token_factory import TokenFactoryClient, get_llm

log = logging.getLogger(__name__)


async def process_pending(llm: TokenFactoryClient | None = None, *, extract: bool = True) -> dict:
    llm = llm or get_llm()
    async with session_scope() as session:
        chunks = await chunk_pending(session)
    async with session_scope() as session:
        embedded = await embed_pending(session, llm)
    stats = {"chunks": chunks, "embedded": embedded}
    if extract:
        async with session_scope() as session:
            stats.update(await extract_pending(session, llm))
        async with session_scope() as session:
            stats.update(await apply_waiting_on_rule(session))
    log.info("pipeline: %s", stats)
    return stats
