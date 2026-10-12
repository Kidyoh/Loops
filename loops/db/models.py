from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from loops.core.config import get_settings

EMBED_DIM = get_settings().embed_dim
TS_CONFIG = "english"

SOURCE_KINDS = ("gmail", "telegram")
COMMITMENT_KINDS = ("i_promised", "they_promised", "waiting_on", "postponed")
COMMITMENT_STATUSES = ("open", "done", "stale")


class Base(DeclarativeBase):
    pass


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    account: Mapped[str] = mapped_column(String(255))
    # Fernet-encrypted credentials (Gmail refresh token). Never stored in plain text.
    credentials: Mapped[bytes | None] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (UniqueConstraint("kind", "account"),)


class SyncState(Base):
    """One row per cursor. Gmail uses key 'history_id'; Telegram uses one key per chat id."""

    __tablename__ = "sync_state"

    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Person(Base):
    __tablename__ = "people"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255))
    emails: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    telegram_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), default=list, server_default="{}")
    aliases: Mapped[list[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    is_me: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    __table_args__ = (
        Index("ix_people_emails", "emails", postgresql_using="gin"),
        Index("ix_people_telegram_ids", "telegram_ids", postgresql_using="gin"),
    )


class Thread(Base):
    __tablename__ = "threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(Text)
    participants: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default="{}")
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[Source] = relationship()

    __table_args__ = (UniqueConstraint("source_id", "external_id"),)


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    external_id: Mapped[str] = mapped_column(String(255))
    sender_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    is_from_me: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    body_text: Mapped[str] = mapped_column(Text, default="")
    url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSONB)
    tsv = mapped_column(TSVECTOR, Computed(f"to_tsvector('{TS_CONFIG}', coalesce(body_text, ''))", persisted=True))
    # Pipeline bookkeeping: NULL means the step still has to run for this message.
    chunked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    thread: Mapped[Thread] = relationship()
    sender: Mapped[Person | None] = relationship()

    __table_args__ = (
        UniqueConstraint("thread_id", "external_id"),
        Index("ix_messages_sent_at", "sent_at"),
        Index("ix_messages_thread_sent_at", "thread_id", "sent_at"),
        Index("ix_messages_tsv", "tsv", postgresql_using="gin"),
    )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    # First message of the window; message_ids holds every message covered (for citations).
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    message_ids: Mapped[list[int]] = mapped_column(ARRAY(Integer), default=list, server_default="{}")
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    tsv = mapped_column(TSVECTOR, Computed(f"to_tsvector('{TS_CONFIG}', text)", persisted=True))
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chunks_tsv", "tsv", postgresql_using="gin"),
        Index("ix_chunks_thread", "thread_id"),
        Index("ix_chunks_message_ids", "message_ids", postgresql_using="gin"),
    )


class Commitment(Base):
    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"))
    summary: Mapped[str] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(8), default="open", server_default="open")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    # 'llm' for model extraction, 'rule' for the no-reply waiting_on heuristic.
    origin: Mapped[str] = mapped_column(String(8), default="llm", server_default="llm")
    source_message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    person: Mapped[Person | None] = relationship()
    source_message: Mapped[Message | None] = relationship()

    __table_args__ = (
        Index("ix_commitments_status_kind", "status", "kind"),
        Index("ix_commitments_thread", "thread_id"),
    )
