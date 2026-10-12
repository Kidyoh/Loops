import pytest
from sqlalchemy import text

pytestmark = pytest.mark.db


async def test_schema_and_vector_index(db):
    rows = (await db.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'chunks'"))).scalars().all()
    assert "ix_chunks_embedding_hnsw" in rows
    ext = (await db.execute(text("SELECT extname FROM pg_extension WHERE extname = 'vector'"))).scalar()
    assert ext == "vector"
