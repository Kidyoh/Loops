import asyncio
import json
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from loops.agent.agent import run_agent
from loops.agent.queries import get_thread_messages, list_commitments
from loops.agent.search import find_people
from loops.api.auth import COOKIE, check_password, require_auth, session_token
from loops.core.config import get_settings
from loops.db.models import Commitment, Message, Source, SyncState, Thread
from loops.db.session import dispose_engine, session_scope
from loops.ingest import gmail
from loops.llm.token_factory import get_llm
from loops.worker.jobs import sync_all

log = logging.getLogger(__name__)
OAUTH_STATE_COOKIE = "gmail_oauth_state"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not get_settings().web_password:
        log.warning("WEB_PASSWORD is not set: the API is open to anyone who can reach it")
    yield
    await dispose_engine()


app = FastAPI(title="Loops", lifespan=lifespan)
_background: set[asyncio.Task] = set()


async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


Auth = Depends(require_auth)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


# --- auth -----------------------------------------------------------------------------


class LoginIn(BaseModel):
    password: str


@app.post("/login")
async def login(body: LoginIn, response: Response) -> dict:
    if not check_password(body.password):
        raise HTTPException(status_code=401, detail="wrong password")
    response.set_cookie(
        COOKIE, session_token(get_settings().web_password), httponly=True, samesite="lax", max_age=30 * 86400
    )
    return {"ok": True}


@app.post("/logout")
async def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE)
    return {"ok": True}


# --- ask ------------------------------------------------------------------------------


class AskIn(BaseModel):
    question: str
    history: list[dict] = []
    mode: Literal["tools", "router"] | None = None


@app.post("/ask", dependencies=[Auth])
async def ask(body: AskIn) -> EventSourceResponse:
    async def events():
        async with session_scope() as session:
            try:
                async for event in run_agent(session, get_llm(), body.question, body.history, body.mode):
                    kind = event.pop("type")
                    yield {"event": kind, "data": json.dumps(event)}
            except Exception as e:
                log.exception("ask failed")
                yield {"event": "error", "data": json.dumps({"message": str(e)})}
        yield {"event": "done", "data": "{}"}

    return EventSourceResponse(events())


# --- commitments ----------------------------------------------------------------------


@app.get("/commitments", dependencies=[Auth])
async def get_commitments(
    kind: str | None = None,
    status: str | None = "open",
    person: str | None = None,
    session: AsyncSession = Depends(db),
) -> list[dict]:
    person_ids = None
    if person:
        person_ids = [int(person)] if person.isdigit() else [p.id for p in await find_people(session, person)] or [-1]
    items = await list_commitments(
        session, kind=kind, status=None if status == "any" else status, person_ids=person_ids, limit=500
    )
    return [c.as_dict() for c in items]


class CommitmentPatch(BaseModel):
    status: Literal["open", "done", "stale"]


@app.patch("/commitments/{commitment_id}", dependencies=[Auth])
async def patch_commitment(commitment_id: int, body: CommitmentPatch, session: AsyncSession = Depends(db)) -> dict:
    c = await session.get(Commitment, commitment_id)
    if c is None:
        raise HTTPException(status_code=404, detail="commitment not found")
    c.status = body.status
    return {"id": c.id, "status": c.status}


# --- messages -------------------------------------------------------------------------


@app.get("/messages/{message_id}", dependencies=[Auth])
async def get_message(message_id: int, session: AsyncSession = Depends(db)) -> dict:
    m = await session.get(Message, message_id)
    if m is None:
        raise HTTPException(status_code=404, detail="message not found")
    thread, context = await get_thread_messages(session, m.thread_id, m.id, window=5)

    def view(x: Message) -> dict:
        return {
            "id": x.id,
            "sender": "Me" if x.is_from_me else (x.sender.display_name if x.sender else None),
            "is_from_me": x.is_from_me,
            "sent_at": x.sent_at.isoformat(),
            "body_text": x.body_text,
            "url": x.url,
        }

    return {
        "message": next((view(x) for x in context if x.id == m.id), None),
        "thread": {"id": thread.id, "title": thread.title, "source": thread.source.kind},
        "context": [view(x) for x in context],
    }


# --- sources --------------------------------------------------------------------------


@app.get("/sources", dependencies=[Auth])
async def get_sources(session: AsyncSession = Depends(db)) -> list[dict]:
    last_sync = select(SyncState.source_id, func.max(SyncState.last_synced_at).label("last")).group_by(
        SyncState.source_id
    ).subquery()
    counts = (
        select(Thread.source_id, func.count(Message.id).label("n"))
        .join(Message, Message.thread_id == Thread.id)
        .group_by(Thread.source_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Source, last_sync.c.last, counts.c.n)
            .outerjoin(last_sync, last_sync.c.source_id == Source.id)
            .outerjoin(counts, counts.c.source_id == Source.id)
            .order_by(Source.id)
        )
    ).all()
    out = [
        {
            "id": s.id,
            "kind": s.kind,
            "account": s.account,
            "connected": s.kind == "telegram" or s.credentials is not None,
            "last_synced_at": last.isoformat() if last else None,
            "messages": n or 0,
        }
        for s, last, n in rows
    ]
    if not any(s["kind"] == "telegram" for s in out):
        out.append(
            {
                "id": None,
                "kind": "telegram",
                "account": None,
                "connected": Path(get_settings().tg_session_path).exists(),
                "last_synced_at": None,
                "messages": 0,
            }
        )
    return out


@app.post("/sources/gmail/connect", dependencies=[Auth])
async def gmail_connect(response: Response) -> dict:
    state = secrets.token_urlsafe(24)
    response.set_cookie(OAUTH_STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600)
    return {"url": gmail.authorization_url(state)}


@app.get("/sources/gmail/callback", dependencies=[Auth])
async def gmail_callback(request: Request, code: str, state: str) -> dict:
    expected = request.cookies.get(OAUTH_STATE_COOKIE)
    if not expected or not secrets.compare_digest(expected, state):
        raise HTTPException(status_code=400, detail="OAuth state mismatch")
    try:
        source = await gmail.exchange_code(code)
    except gmail.GmailError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"connected": source.account}


@app.delete("/sources/{source_id}", dependencies=[Auth])
async def delete_source(source_id: int, session: AsyncSession = Depends(db)) -> dict:
    """Wipe a source and every derived row (threads, messages, chunks, commitments)."""
    source = await session.get(Source, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="source not found")
    if source.kind == "telegram":
        Path(get_settings().tg_session_path).unlink(missing_ok=True)
    await session.delete(source)
    return {"deleted": source_id}


@app.post("/sync", dependencies=[Auth])
async def trigger_sync() -> dict:
    task = asyncio.create_task(sync_all())
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"started": True}
