import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

DIM = 8


def fake_vector(text: str) -> list[float]:
    h = hashlib.sha256(text.lower().encode()).digest()
    return [b / 255 for b in h[:DIM]]


class FakeLLM:
    """Stands in for TokenFactoryClient. Chat replies come from a queue or a callable."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls: list[dict] = []

    async def embed(self, texts):
        return [fake_vector(t) for t in texts]

    async def chat(self, messages, **kw):
        self.calls.append({"messages": messages, **kw})
        reply = self.replies.pop(0) if self.replies else {"role": "assistant", "content": ""}
        if callable(reply):
            reply = reply(messages, kw)
        if isinstance(reply, str):
            reply = {"role": "assistant", "content": reply}
        return reply

    async def chat_json(self, messages, **kw):
        from loops.llm.token_factory import parse_json_loose

        msg = await self.chat(messages, **kw)
        content = msg["content"]
        return parse_json_loose(content) if isinstance(content, str) else content

    async def chat_stream(self, messages, **kw):
        msg = await self.chat(messages, **kw)
        for word in msg["content"].split(" "):
            yield word + " "


def dt(day: int, hour: int = 12, minute: int = 0, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=UTC)


# --- Telethon stand-ins ---------------------------------------------------------------


def tg_user(id, first, last=None, username=None, bot=False, is_self=False):
    return SimpleNamespace(id=id, first_name=first, last_name=last, username=username, bot=bot, is_self=is_self)


def tg_group(id, title, megagroup=False, username=None, broadcast=False):
    return SimpleNamespace(id=id, title=title, megagroup=megagroup, username=username, broadcast=broadcast)


def tg_msg(id, date, text, sender, out=False):
    return SimpleNamespace(id=id, date=date, message=text, sender=sender, sender_id=sender.id, out=out, reply_to=None)


def tg_dialog(id, entity, date, is_user=False, is_group=False, muted=False, pinned=False):
    mute_until = datetime(2099, 1, 1, tzinfo=UTC) if muted else None
    return SimpleNamespace(
        id=id,
        entity=entity,
        date=date,
        is_user=is_user,
        is_group=is_group,
        pinned=pinned,
        dialog=SimpleNamespace(notify_settings=SimpleNamespace(mute_until=mute_until)),
    )


class FakeTelegramClient:
    def __init__(self, me, dialogs, messages):
        self.me = me
        self.dialogs = dialogs
        self.messages = messages  # chat id -> list of tg_msg

    async def get_me(self):
        return self.me

    async def iter_dialogs(self):
        for d in self.dialogs:
            yield d

    async def get_messages(self, entity, from_user=None, limit=None):
        msgs = self._for(entity)
        if from_user == "me":
            msgs = [m for m in msgs if m.out]
        return msgs[:limit]

    async def iter_messages(self, entity, min_id=0, offset_date=None, reverse=False):
        for m in sorted(self._for(entity), key=lambda m: m.id):
            if min_id and m.id <= min_id:
                continue
            if offset_date and not min_id and m.date < offset_date:
                continue
            yield m

    def _for(self, entity):
        for d in self.dialogs:
            if d.entity is entity:
                return self.messages.get(d.id, [])
        return []
