from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class SenderRef:
    name: str
    email: str | None = None
    telegram_id: int | None = None


@dataclass
class IncomingMessage:
    """Source-agnostic message produced by an ingester, before it touches the DB."""

    thread_external_id: str
    external_id: str
    sender: SenderRef
    is_from_me: bool
    sent_at: datetime
    body_text: str
    thread_title: str | None = None
    url: str | None = None
    # Other people on the thread (email To/Cc). The sender is added automatically.
    participants: list[SenderRef] = field(default_factory=list)
    raw: dict | None = None
