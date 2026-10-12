"""Single-user auth: WEB_PASSWORD, exchanged for an HMAC session cookie or sent as a Bearer token."""

import hashlib
import hmac
import logging

from fastapi import HTTPException, Request

from loops.core.config import get_settings

log = logging.getLogger(__name__)

COOKIE = "loops_session"


def session_token(password: str) -> str:
    return hmac.new(password.encode(), b"loops-session-v1", hashlib.sha256).hexdigest()


def check_password(candidate: str) -> bool:
    password = get_settings().web_password
    return bool(password) and hmac.compare_digest(candidate.encode(), password.encode())


async def require_auth(request: Request) -> None:
    password = get_settings().web_password
    if not password:
        return  # auth disabled for local development; warned at startup
    cookie = request.cookies.get(COOKIE)
    if cookie and hmac.compare_digest(cookie, session_token(password)):
        return
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer ") and check_password(header[7:]):
        return
    raise HTTPException(status_code=401, detail="not authenticated")
