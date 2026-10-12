"""Gmail ingest through the Gmail REST API with OAuth (testing mode, your account only).

Backfill lists the last BACKFILL_DAYS of mail; incremental sync follows history.list
from the stored historyId. The refresh token is Fernet-encrypted in sources.credentials.
"""

import asyncio
import base64
import html
import logging
import re
from datetime import UTC, datetime
from email.utils import getaddresses, parseaddr
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from loops.core.config import Settings, get_settings
from loops.core.crypto import decrypt, encrypt
from loops.db.models import Source
from loops.db.session import session_scope
from loops.ingest.normalize import clean_email_body, get_or_create_source, resolve_person, store_messages
from loops.ingest.state import get_state, set_state
from loops.ingest.types import IncomingMessage, SenderRef

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://gmail.googleapis.com/gmail/v1/users/me/"
SCOPES = "https://www.googleapis.com/auth/gmail.readonly"
SKIP_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "SPAM", "TRASH", "DRAFT"}
FETCH_CONCURRENCY = 8
STORE_BATCH = 100
HISTORY_KEY = "history_id"


class GmailError(RuntimeError):
    pass


# --- OAuth ----------------------------------------------------------------------------


def authorization_url(state: str, s: Settings | None = None) -> str:
    s = s or get_settings()
    params = {
        "client_id": s.gmail_client_id,
        "redirect_uri": s.gmail_redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # always return a refresh token
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str, http: httpx.AsyncClient | None = None) -> Source:
    """Finish OAuth: store the encrypted refresh token on the gmail source."""
    s = get_settings()
    async with _maybe_client(http) as client:
        r = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": s.gmail_client_id,
                "client_secret": s.gmail_client_secret,
                "redirect_uri": s.gmail_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        if r.status_code >= 400:
            raise GmailError(f"token exchange failed: {r.text[:300]}")
        tokens = r.json()
        refresh = tokens.get("refresh_token")
        if not refresh:
            raise GmailError("no refresh_token returned; remove the app grant in your Google account and retry")
        profile = await client.get(API + "profile", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        profile.raise_for_status()
        email = profile.json()["emailAddress"].lower()

    async with session_scope() as session:
        source = await get_or_create_source(session, "gmail", email)
        source.credentials = encrypt(refresh.encode())
        await resolve_person(session, SenderRef(name="", email=email), is_me=True)
    return source


class _maybe_client:
    def __init__(self, http: httpx.AsyncClient | None):
        self.http = http
        self.owned = http is None

    async def __aenter__(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(timeout=60)
        return self.http

    async def __aexit__(self, *exc) -> None:
        if self.owned and self.http is not None:
            await self.http.aclose()


class GmailClient:
    def __init__(self, refresh_token: str, http: httpx.AsyncClient, s: Settings | None = None):
        self.s = s or get_settings()
        self.refresh_token = refresh_token
        self.http = http
        self.access_token: str | None = None

    async def _refresh(self) -> None:
        r = await self.http.post(
            TOKEN_URL,
            data={
                "client_id": self.s.gmail_client_id,
                "client_secret": self.s.gmail_client_secret,
                "refresh_token": self.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code >= 400:
            raise GmailError(f"token refresh failed ({r.status_code}); reconnect Gmail")
        self.access_token = r.json()["access_token"]

    async def get(self, path: str, params: dict | None = None) -> dict:
        if self.access_token is None:
            await self._refresh()
        delay = 1.0
        for attempt in range(5):
            r = await self.http.get(API + path, params=params, headers={"Authorization": f"Bearer {self.access_token}"})
            if r.status_code == 401 and attempt == 0:
                await self._refresh()
                continue
            if r.status_code in (429, 500, 503) and attempt < 4:
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if r.status_code >= 400:
                raise GmailError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
            return r.json()
        raise GmailError(f"GET {path}: retries exhausted")


# --- parsing --------------------------------------------------------------------------


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk(part: dict) -> list[dict]:
    parts = [part]
    for child in part.get("parts") or []:
        parts.extend(_walk(child))
    return parts


def html_to_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|head).*?</\1>", "", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</li>", "\n", raw)
    # Gmail wraps quoted history in gmail_quote blocks; the text cleaner drops it later.
    raw = re.sub(r"(?s)<[^>]+>", "", raw)
    text = html.unescape(raw).replace("\xa0", " ")
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def extract_body(payload: dict) -> str:
    parts = [p for p in _walk(payload) if not (p.get("filename") or "")]
    for mime in ("text/plain", "text/html"):
        for p in parts:
            data = (p.get("body") or {}).get("data")
            if p.get("mimeType") == mime and data:
                text = _b64(data)
                return text if mime == "text/plain" else html_to_text(text)
    return ""


def parse_message(msg: dict, account_email: str) -> IncomingMessage | None:
    labels = set(msg.get("labelIds") or [])
    if labels & SKIP_LABELS:
        return None
    payload = msg.get("payload") or {}
    headers = {h["name"].lower(): h["value"] for h in payload.get("headers") or []}
    from_name, from_email = parseaddr(headers.get("from", ""))
    from_email = from_email.lower()
    is_from_me = "SENT" in labels or from_email == account_email.lower()
    others = [
        SenderRef(name=name, email=addr.lower())
        for name, addr in getaddresses([headers.get("to", ""), headers.get("cc", "")])
        if addr and addr.lower() != account_email.lower()
    ]
    body = clean_email_body(extract_body(payload))
    if not body:
        body = html.unescape(msg.get("snippet") or "")
    sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, UTC)
    return IncomingMessage(
        thread_external_id=msg["threadId"],
        thread_title=headers.get("subject") or "(no subject)",
        external_id=msg["id"],
        sender=SenderRef(name=from_name, email=from_email or None),
        is_from_me=is_from_me,
        sent_at=sent_at,
        body_text=body,
        url=f"https://mail.google.com/mail/u/0/#all/{msg['id']}",
        participants=others,
        raw={
            "labels": sorted(labels),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "cc": headers.get("cc"),
            "message_id": headers.get("message-id"),
            "snippet": msg.get("snippet"),
        },
    )


# --- sync -----------------------------------------------------------------------------


async def _list_ids_backfill(client: GmailClient, days: int) -> list[str]:
    ids: list[str] = []
    params: dict[str, Any] = {"q": f"newer_than:{days}d -in:chats -category:promotions -category:social", "maxResults": 500}
    while True:
        page = await client.get("messages", params)
        ids.extend(m["id"] for m in page.get("messages") or [])
        token = page.get("nextPageToken")
        if not token:
            return ids
        params["pageToken"] = token


async def _list_ids_history(client: GmailClient, start: str) -> tuple[list[str], str] | None:
    """Return (message ids added since start, new history id), or None if start is too old."""
    ids: list[str] = []
    params: dict[str, Any] = {"startHistoryId": start, "historyTypes": "messageAdded", "maxResults": 500}
    latest = start
    while True:
        try:
            page = await client.get("history", params)
        except GmailError as e:
            if "-> 404" in str(e):
                return None
            raise
        latest = page.get("historyId", latest)
        for h in page.get("history") or []:
            for added in h.get("messagesAdded") or []:
                ids.append(added["message"]["id"])
        token = page.get("nextPageToken")
        if not token:
            return list(dict.fromkeys(ids)), latest
        params["pageToken"] = token


async def _fetch_all(client: GmailClient, ids: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def one(mid: str) -> dict | None:
        async with sem:
            try:
                return await client.get(f"messages/{mid}", {"format": "full"})
            except GmailError as e:
                if "-> 404" in str(e):
                    return None  # deleted between list and fetch
                raise

    results = await asyncio.gather(*(one(i) for i in ids))
    return [r for r in results if r]


async def sync_gmail_source(source_id: int, http: httpx.AsyncClient | None = None) -> int:
    s = get_settings()
    async with session_scope() as session:
        source = await session.get(Source, source_id)
        if source is None or not source.credentials:
            raise GmailError("Gmail source is not connected")
        account = source.account
        refresh = decrypt(source.credentials).decode()
        state = await get_state(session, source_id, HISTORY_KEY)
        cursor = state.cursor if state else None

    async with _maybe_client(http) as h:
        client = GmailClient(refresh, h, s)
        listing = await _list_ids_history(client, cursor) if cursor else None
        if listing is None:
            if cursor:
                log.warning("gmail history %s expired; re-listing last %d days", cursor, s.backfill_days)
            new_cursor = (await client.get("profile"))["historyId"]
            ids = await _list_ids_backfill(client, s.backfill_days)
        else:
            ids, new_cursor = listing

        total = 0
        for i in range(0, len(ids), STORE_BATCH):
            raw = await _fetch_all(client, ids[i : i + STORE_BATCH])
            items = [m for m in (parse_message(r, account) for r in raw) if m]
            async with session_scope() as session:
                source = await session.get(Source, source_id)
                total += await store_messages(session, source, items)

    async with session_scope() as session:
        await set_state(session, source_id, HISTORY_KEY, str(new_cursor))
    log.info("gmail %s: %d new", account, total)
    return total


async def sync_gmail(http: httpx.AsyncClient | None = None) -> int:
    async with session_scope() as session:
        ids = (
            await session.execute(select(Source.id).where(Source.kind == "gmail", Source.credentials.is_not(None)))
        ).scalars().all()
    total = 0
    for sid in ids:
        total += await sync_gmail_source(sid, http)
    return total
