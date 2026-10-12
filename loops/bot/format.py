"""Telegram HTML formatting for answers, open-loop lists and the daily digest."""

import html
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from loops.agent.agent import CITATION_RE
from loops.agent.queries import CommitmentView
from loops.core.config import get_settings

TG_LIMIT = 4096

KIND_TITLES = {
    "i_promised": "I promised",
    "they_promised": "They promised",
    "waiting_on": "Waiting on",
    "postponed": "Postponed",
}


def _link(text: str, url: str | None) -> str:
    text = html.escape(text)
    return f'<a href="{html.escape(url, quote=True)}">{text}</a>' if url else text


def format_answer(answer: str, citations: list[dict]) -> str:
    """Replace [source: id] markers with numbered refs and list the sources below."""
    numbers: dict[int, int] = {}
    by_id = {c["message_id"]: c for c in citations}

    def ref(match: re.Match) -> str:
        refs = []
        for part in match.group(1).split(","):
            part = part.strip()
            if part.isdigit() and int(part) in by_id:
                n = numbers.setdefault(int(part), len(numbers) + 1)
                refs.append(f"[{n}]")
        return "".join(refs)

    # Escape around the markers so the numbered refs survive as plain text.
    pieces, last = [], 0
    for m in CITATION_RE.finditer(answer):
        pieces.append(html.escape(answer[last : m.start()]))
        pieces.append(ref(m))
        last = m.end()
    pieces.append(html.escape(answer[last:]))
    body = re.sub(r"[ \t]+(\n|$)", r"\1", "".join(pieces)).strip()

    if numbers:
        lines = ["", "<b>Sources</b>"]
        for mid, n in sorted(numbers.items(), key=lambda kv: kv[1]):
            c = by_id[mid]
            label = f"{c['source']} · {c['sender'] or '?'} · {c['sent_at'][:10]}"
            if c.get("thread_title"):
                label += f" · {c['thread_title']}"
            lines.append(f"[{n}] " + _link(label, c.get("url") or c.get("app_url")))
        body += "\n" + "\n".join(lines)
    return truncate(body)


def format_item(c: CommitmentView, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    tz = ZoneInfo(get_settings().timezone)
    parts = [f"<code>#{c.id}</code>", html.escape(c.summary)]
    if c.person:
        parts.append(f"— {html.escape(c.person)}")
    if c.due_at:
        due = c.due_at.astimezone(tz)
        flag = "⚠️ overdue " if c.due_at < now and c.status == "open" else "due "
        parts.append(f"({flag}{due:%a %b %d})")
    if c.sent_at:
        parts.append(_link(f"{c.source} {c.sent_at.astimezone(tz):%b %d}", c.url))
    return " ".join(parts)


def format_loops(title: str, items: list[CommitmentView], empty: str = "Nothing open.") -> str:
    if not items:
        return f"<b>{html.escape(title)}</b>\n{html.escape(empty)}"
    lines = [f"<b>{html.escape(title)}</b> ({len(items)})"]
    lines += [f"• {format_item(c)}" for c in items]
    lines.append("\nMark one done with /done &lt;id&gt;")
    return truncate("\n".join(lines))


def format_digest(
    overdue: list[CommitmentView], due_today: list[CommitmentView], waiting: list[CommitmentView], now: datetime
) -> str | None:
    if not (overdue or due_today or waiting):
        return None
    tz = ZoneInfo(get_settings().timezone)
    lines = [f"<b>Good morning. Open loops for {now.astimezone(tz):%A, %b %d}</b>"]
    for title, items in (("Overdue promises", overdue), ("Due today", due_today), ("Waiting on", waiting)):
        if items:
            lines.append(f"\n<b>{title}</b>")
            lines += [f"• {format_item(c, now)}" for c in items]
    return truncate("\n".join(lines))


def truncate(text: str, limit: int = TG_LIMIT) -> str:
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 20)
    return text[: cut if cut > 0 else limit - 20] + "\n…(truncated)"
