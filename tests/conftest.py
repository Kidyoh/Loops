import os
import subprocess
import sys
from pathlib import Path

import pytest

# Small embeddings keep DB tests fast; must be set before loops modules are imported.
os.environ.setdefault("EMBED_DIM", "8")
os.environ.setdefault("ENCRYPTION_KEY", "4qGqYvDqT8bAPHqvRb1HsJOdRkQ-6s0BUe3m8W9U9cE=")
os.environ.setdefault("CHAT_SYSTEM_PREFIX", "")
TEST_DB = os.environ.get("TEST_DATABASE_URL")
if TEST_DB:
    os.environ["DATABASE_URL"] = TEST_DB

ROOT = Path(__file__).resolve().parents[1]

TABLES = "commitments, chunks, messages, threads, people, sync_state, sources"


@pytest.fixture(scope="session")
def migrated_db():
    if not TEST_DB:
        pytest.skip("TEST_DATABASE_URL not set")
    env = {**os.environ, "DATABASE_URL": TEST_DB}
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, check=True)
    return TEST_DB


@pytest.fixture
async def db(migrated_db):
    from sqlalchemy import text

    from loops.db.session import dispose_engine, get_engine, session_scope

    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    async with session_scope() as session:
        yield session
    await dispose_engine()
