"""Telegram bot: ask questions, list open loops, mark them done. Owner only.

Messages from anyone but OWNER_TELEGRAM_ID match no handler and are ignored.
"""

import asyncio
import html
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from loops.agent.agent import run_agent
from loops.agent.queries import list_commitments
from loops.bot.digest import build_digest
from loops.bot.format import KIND_TITLES, format_answer, format_loops
from loops.core.config import get_settings
from loops.db.models import Commitment
from loops.db.session import session_scope
from loops.llm.token_factory import get_llm
from loops.worker.jobs import sync_all

log = logging.getLogger(__name__)

EDIT_INTERVAL_S = 1.5
HISTORY_TURNS = 3
MAX_BUTTONS = 8

HELP = """<b>Loops</b>: ask anything about your Gmail and Telegram.

/promises: what I promised
/waiting: who I'm waiting on
/postponed: what got moved
/theypromised: what others promised me
/done &lt;id&gt;: mark an item done
/digest: today's digest now
/sync: sync sources now

Or just type a question, e.g. <i>What did Robi say about the Habesha pitch?</i>"""

COMMAND_KINDS = {
    "promises": "i_promised",
    "waiting": "waiting_on",
    "postponed": "postponed",
    "theypromised": "they_promised",
}


# --- core logic (no Telegram objects; easy to test) -----------------------------------


async def loops_reply(kind: str) -> tuple[str, list[int]]:
    async with session_scope() as session:
        items = await list_commitments(session, kind=kind, status="open", limit=40)
    return format_loops(KIND_TITLES[kind], items), [c.id for c in items[:MAX_BUTTONS]]


async def mark_done(commitment_id: int) -> str:
    async with session_scope() as session:
        c = await session.get(Commitment, commitment_id)
        if c is None:
            return f"No item #{commitment_id}."
        if c.status == "done":
            return f"#{c.id} was already done."
        c.status = "done"
        return f"✅ Done: #{c.id} {html.escape(c.summary)}"


def done_keyboard(ids: list[int]) -> InlineKeyboardMarkup | None:
    if not ids:
        return None
    buttons = [InlineKeyboardButton(f"✅ #{i}", callback_data=f"done:{i}") for i in ids]
    return InlineKeyboardMarkup([buttons[i : i + 4] for i in range(0, len(buttons), 4)])


# --- handlers -------------------------------------------------------------------------


async def help_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_html(HELP)


async def loops_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    command = update.message.text.split()[0].lstrip("/").split("@")[0].lower()
    text, ids = await loops_reply(COMMAND_KINDS[command])
    await update.message.reply_html(text, disable_web_page_preview=True, reply_markup=done_keyboard(ids))


async def done_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = [a.lstrip("#") for a in context.args or []]
    if not args or not all(a.isdigit() for a in args):
        await update.message.reply_text("Usage: /done <id>")
        return
    replies = [await mark_done(int(a)) for a in args]
    await update.message.reply_html("\n".join(replies))


async def done_button(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query.from_user.id != get_settings().owner_telegram_id:
        await query.answer()
        return
    reply = await mark_done(int(query.data.split(":", 1)[1]))
    await query.answer(html.unescape(reply)[:200])


async def digest_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as session:
        text = await build_digest(session)
    await update.message.reply_html(text or "Nothing open. 🎉", disable_web_page_preview=True)


async def sync_cmd(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text("Syncing…")
    stats = await sync_all()
    await msg.edit_text("Sync done: " + ", ".join(f"{k} {v}" for k, v in stats.items()))


async def _edit(msg, text: str, **kw) -> None:
    try:
        await msg.edit_text(text, **kw)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def ask_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    question = update.message.text.strip()
    await update.message.chat.send_action(ChatAction.TYPING)
    msg = await update.message.reply_text("Searching your messages…")
    history: list[dict] = context.chat_data.setdefault("history", [])

    answer, citations, last_edit = "", [], time.monotonic()
    try:
        async with session_scope() as session:
            async for event in run_agent(session, get_llm(), question, history[-HISTORY_TURNS * 2 :]):
                if event["type"] == "token":
                    answer += event["text"]
                    if time.monotonic() - last_edit > EDIT_INTERVAL_S and answer.strip():
                        await _edit(msg, answer + " …")
                        last_edit = time.monotonic()
                elif event["type"] == "citations":
                    citations = event["items"]
    except Exception:
        log.exception("ask failed")
        await _edit(msg, "Something went wrong answering that. Try again in a moment.")
        return

    history += [{"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    del history[: -HISTORY_TURNS * 2]
    await _edit(msg, format_answer(answer, citations) or "No answer.", parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def build_app(token: str | None = None, owner_id: int | None = None) -> Application:
    s = get_settings()
    token = token or s.bot_token
    owner_id = owner_id or s.owner_telegram_id
    if not token or not owner_id:
        raise RuntimeError("BOT_TOKEN and OWNER_TELEGRAM_ID must be set")
    owner = filters.User(user_id=owner_id)
    app = Application.builder().token(token).concurrent_updates(True).build()
    app.add_handler(CommandHandler(["start", "help"], help_cmd, filters=owner))
    app.add_handler(CommandHandler(list(COMMAND_KINDS), loops_cmd, filters=owner))
    app.add_handler(CommandHandler("done", done_cmd, filters=owner))
    app.add_handler(CommandHandler("digest", digest_cmd, filters=owner))
    app.add_handler(CommandHandler("sync", sync_cmd, filters=owner))
    app.add_handler(CallbackQueryHandler(done_button, pattern=r"^done:\d+$"))
    app.add_handler(MessageHandler(owner & filters.TEXT & ~filters.COMMAND, ask_handler))
    return app


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.set_event_loop(asyncio.new_event_loop())
    build_app().run_polling(allowed_updates=Update.ALL_TYPES)
