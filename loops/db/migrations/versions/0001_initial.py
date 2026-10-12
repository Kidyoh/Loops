"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-12
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql as pg

from loops.core.config import get_settings
from loops.db.vector import hnsw_index_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

TSZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    dim = get_settings().embed_dim
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("account", sa.String(255), nullable=False),
        sa.Column("credentials", sa.LargeBinary),
        sa.Column("created_at", TSZ, server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("kind", "account"),
    )
    op.create_table(
        "sync_state",
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("cursor", sa.Text),
        sa.Column("last_synced_at", TSZ),
    )
    op.create_table(
        "people",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("emails", pg.ARRAY(sa.String), server_default="{}", nullable=False),
        sa.Column("telegram_ids", pg.ARRAY(sa.BigInteger), server_default="{}", nullable=False),
        sa.Column("aliases", pg.ARRAY(sa.String), server_default="{}", nullable=False),
        sa.Column("is_me", sa.Boolean, server_default="false", nullable=False),
    )
    op.create_index("ix_people_emails", "people", ["emails"], postgresql_using="gin")
    op.create_index("ix_people_telegram_ids", "people", ["telegram_ids"], postgresql_using="gin")

    op.create_table(
        "threads",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("participants", pg.ARRAY(sa.Integer), server_default="{}", nullable=False),
        sa.Column("last_message_at", TSZ),
        sa.UniqueConstraint("source_id", "external_id"),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("thread_id", sa.Integer, sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("sender_id", sa.Integer, sa.ForeignKey("people.id", ondelete="SET NULL")),
        sa.Column("is_from_me", sa.Boolean, nullable=False),
        sa.Column("sent_at", TSZ, nullable=False),
        sa.Column("body_text", sa.Text, nullable=False),
        sa.Column("url", sa.Text),
        sa.Column("raw", pg.JSONB),
        sa.Column(
            "tsv",
            pg.TSVECTOR,
            sa.Computed("to_tsvector('english', coalesce(body_text, ''))", persisted=True),
        ),
        sa.Column("chunked_at", TSZ),
        sa.Column("extracted_at", TSZ),
        sa.UniqueConstraint("thread_id", "external_id"),
    )
    op.create_index("ix_messages_sent_at", "messages", ["sent_at"])
    op.create_index("ix_messages_thread_sent_at", "messages", ["thread_id", "sent_at"])
    op.create_index("ix_messages_tsv", "messages", ["tsv"], postgresql_using="gin")

    op.create_table(
        "chunks",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("message_id", sa.Integer, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message_ids", pg.ARRAY(sa.Integer), server_default="{}", nullable=False),
        sa.Column("thread_id", sa.Integer, sa.ForeignKey("threads.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("embedding", Vector(dim)),
        sa.Column("tsv", pg.TSVECTOR, sa.Computed("to_tsvector('english', text)", persisted=True)),
        sa.Column("sent_at", TSZ, nullable=False),
        sa.Column("created_at", TSZ, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_chunks_tsv", "chunks", ["tsv"], postgresql_using="gin")
    op.create_index("ix_chunks_thread", "chunks", ["thread_id"])
    op.create_index("ix_chunks_message_ids", "chunks", ["message_ids"], postgresql_using="gin")
    op.execute(hnsw_index_sql(dim))

    op.create_table(
        "commitments",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("person_id", sa.Integer, sa.ForeignKey("people.id", ondelete="SET NULL")),
        sa.Column("thread_id", sa.Integer, sa.ForeignKey("threads.id", ondelete="CASCADE")),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("due_at", TSZ),
        sa.Column("status", sa.String(8), server_default="open", nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("origin", sa.String(8), server_default="llm", nullable=False),
        sa.Column("source_message_id", sa.Integer, sa.ForeignKey("messages.id", ondelete="CASCADE")),
        sa.Column("created_at", TSZ, server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", TSZ, server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_commitments_status_kind", "commitments", ["status", "kind"])
    op.create_index("ix_commitments_thread", "commitments", ["thread_id"])


def downgrade() -> None:
    for table in ("commitments", "chunks", "messages", "threads", "people", "sync_state", "sources"):
        op.drop_table(table)
