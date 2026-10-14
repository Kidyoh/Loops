"""Demo dataset: two weeks of realistic Gmail and Telegram conversations.

Everything is fictional. Dates are relative to "now" so the demo always looks recent.
Each message has a stable key "<thread>/<msg>" used by the eval set.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from loops.ingest.types import IncomingMessage, SenderRef

TELEGRAM_ACCOUNT = "demo-telegram"
GMAIL_ACCOUNT = "abel@loops-demo.example"

ME_TG = SenderRef("Abel Tadesse", telegram_id=1000)
ME_MAIL = SenderRef("Abel Tadesse", email=GMAIL_ACCOUNT)

ROBI_TG = SenderRef("Robi Tesfaye", telegram_id=1001)
DAWIT_TG = SenderRef("Dawit Alemu", telegram_id=1002)
MERON_TG = SenderRef("Meron Kebede", telegram_id=1003)

ROBI_MAIL = SenderRef("Robi Tesfaye", email="robi@habesha-labs.example")
SARA = SenderRef("Sara Bekele", email="sara@bekele-law.example")
HANNA = SenderRef("Hanna Girma", email="hanna@gebeya-ventures.example")
LIYA = SenderRef("Liya Haile", email="liya@aiweek-addis.example")
VENUE = SenderRef("Ice Addis Events", email="events@iceaddis.example")
BILLING = SenderRef("CloudHost Billing", email="billing@cloudhost.example")

DEMO_TELEGRAM_IDS = {1000, 1001, 1002, 1003}
DEMO_EMAIL_SUFFIX = ".example"


@dataclass
class DemoMessage:
    key: str  # "<thread>/<msg>", stable across runs
    days_ago: float
    sender: SenderRef
    text: str
    hour: int = 10


@dataclass
class DemoThread:
    source: str  # "telegram" | "gmail"
    external_id: str
    title: str
    messages: list[DemoMessage]
    participants: list[SenderRef] = field(default_factory=list)


def _m(key: str, days_ago: float, sender: SenderRef, text: str, hour: int = 10) -> DemoMessage:
    return DemoMessage(key, days_ago, sender, text, hour)


THREADS: list[DemoThread] = [
    DemoThread("telegram", "1001", "Robi Tesfaye", [
        _m("robi/1", 13, ROBI_TG, "Did you get a chance to look at the Habesha pitch deck? The investor meeting is on the 24th.", 9),
        _m("robi/2", 13, ME_TG, "Not yet, sorry. I'll send you detailed comments on the deck by Friday.", 10),
        _m("robi/3", 12, ROBI_TG, "Thanks. Also, can you intro me to Hanna from Gebeya Ventures? I think she'd like the pitch.", 18),
        _m("robi/4", 12, ME_TG, "Sure, I'll do the intro to Hanna this week.", 19),
        _m("robi/5", 6, ROBI_TG, "Let's move our weekly sync to next Tuesday, I'm traveling to Bahir Dar.", 8),
        _m("robi/6", 6, ME_TG, "Works for me, safe travels.", 9),
    ]),
    DemoThread("telegram", "1002", "Dawit Alemu", [
        _m("dawit/1", 10, ME_TG, "Hey Dawit, can you send the final logo files in SVG and PNG?", 11),
        _m("dawit/2", 10, DAWIT_TG, "Yes, I'll send them tomorrow evening.", 12),
        _m("dawit/3", 4, ME_TG, "Any update on the logo files? I need them for the pitch deck.", 15),
    ]),
    DemoThread("telegram", "-2001", "Habesha Pitch Team", [
        _m("team/1", 9, MERON_TG, "Demo day rehearsal is Thursday 5pm at Ice Addis.", 10),
        _m("team/2", 9, ME_TG, "Great, I'll book the projector for the rehearsal.", 10),
        _m("team/3", 9, DAWIT_TG, "I'll have the new slides ready by Wednesday.", 11),
        _m("team/4", 8, ROBI_TG, "Heads up: rehearsal is pushed to Saturday, the venue was double booked.", 16),
        _m("team/5", 8, MERON_TG, "ok 👍", 16),
    ], participants=[ROBI_TG, DAWIT_TG, MERON_TG]),
    DemoThread("telegram", "1003", "Meron Kebede", [
        _m("meron/1", 3, MERON_TG, "Are you coming to Selam's wedding on the 25th?", 20),
        _m("meron/2", 3, ME_TG, "Yes! I'll confirm whether I'm bringing a plus one by Monday.", 21),
    ]),
    DemoThread("gmail", "g-shareholder", "Shareholder agreement draft", [
        _m("sara/1", 11, ME_MAIL, "Hi Sara,\n\nCould you send the revised shareholder agreement with the vesting changes we discussed?\n\nThanks,\nAbel", 9),
        _m("sara/2", 9, SARA, "Hi Abel,\n\nI'll send the revised draft early next week. Can you confirm the cliff is 12 months?\n\nBest,\nSara", 14),
        _m("sara/3", 9, ME_MAIL, "Confirmed: 12 month cliff, 4 year vesting.", 16),
    ], participants=[SARA]),
    DemoThread("gmail", "g-hanna", "Follow-up: Loops pilot", [
        _m("hanna/1", 7, HANNA, "Hi Abel,\n\nGreat meeting today. Please send the financial model and your current MRR numbers when you can.\n\nHanna", 17),
        _m("hanna/2", 7, ME_MAIL, "Thanks Hanna! I'll send the financial model and MRR numbers by Thursday.", 18),
        _m("hanna/3", 2, HANNA, "Hi Abel, gentle reminder on the financial model. Our partner meeting is next Monday.", 9),
    ], participants=[HANNA]),
    DemoThread("gmail", "g-venue", "Booking change for Saturday", [
        _m("venue/1", 8, ME_MAIL, "Hello,\n\nCan we move our room booking from Thursday to Saturday, same time (5pm)? Please let me know if that works.\n\nAbel", 17),
    ], participants=[VENUE]),
    DemoThread("gmail", "g-budget", "Q4 budget", [
        _m("budget/1", 5, ROBI_MAIL, "Attached the Q4 budget. Can you review it before Friday?", 10),
        _m("budget/2", 5, ME_MAIL, "Will review it tomorrow.", 12),
        _m("budget/3", 4, ME_MAIL, "Reviewed: looks good. Two comments on the marketing spend, it's 30% higher than Q3 and I'd cap ads at 50k birr.", 11),
    ], participants=[ROBI_MAIL]),
    DemoThread("gmail", "g-aiweek", "Speaker invite: AI Week Addis", [
        _m("liya/1", 6, LIYA, "Dear Abel,\n\nWould you join our panel on private AI on October 30 at 2pm?\n\nLiya", 10),
        _m("liya/2", 5, ME_MAIL, "Happy to join. I'll send my bio and headshot by Wednesday.", 9),
        _m("liya/3", 1, LIYA, "Thanks Abel, got your bio and headshot. See you on the 30th!", 15),
    ], participants=[LIYA]),
    DemoThread("gmail", "g-invoice", "Invoice #2041: October hosting", [
        _m("invoice/1", 2, BILLING, "Your invoice #2041 for October hosting is ready. Amount due: $84.20. Paid automatically by card ending 4421.", 6),
    ]),
]


def build_messages(now: datetime | None = None) -> dict[str, list[IncomingMessage]]:
    """IncomingMessages per source kind, dated relative to now."""
    now = now or datetime.now(UTC)
    out: dict[str, list[IncomingMessage]] = {"telegram": [], "gmail": []}
    for thread in THREADS:
        for m in thread.messages:
            day = (now - timedelta(days=m.days_ago)).date()
            # Hours are Addis Ababa local time (UTC+3).
            sent_at = datetime(day.year, day.month, day.day, m.hour, tzinfo=UTC) - timedelta(hours=3)
            sent_at = min(sent_at, now - timedelta(minutes=5))
            is_me = m.sender in (ME_TG, ME_MAIL)
            out[thread.source].append(
                IncomingMessage(
                    thread_external_id=thread.external_id,
                    external_id=m.key,
                    sender=m.sender,
                    is_from_me=is_me,
                    sent_at=sent_at,
                    body_text=m.text,
                    thread_title=thread.title,
                    participants=thread.participants,
                    raw={"demo": True},
                )
            )
    return out
