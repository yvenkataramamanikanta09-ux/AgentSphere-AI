"""Argon2 passwords and opaque, database-backed sessions."""

import hashlib
import re
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import BoundedSemaphore
from typing import Annotated
from urllib.parse import urlsplit

from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.orm import Session

from agentsphere.database import get_session
from agentsphere.models import AuthSession, User

DatabaseSession = Annotated[Session, Depends(get_session, scope="function")]
PASSWORD_HASHER = PasswordHasher(memory_cost=65536, time_cost=3, parallelism=1, type=Type.ID)
DUMMY_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))
PASSWORD_SLOTS = BoundedSemaphore(2)
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
CSRF_HEADER = APIKeyHeader(name="X-CSRF-Token", auto_error=False, scheme_name="CSRF")
CsrfHeader = Annotated[str | None, Depends(CSRF_HEADER)]


def now(request: Request) -> datetime:
    """Injectable clock for deterministic expiry tests."""
    return request.app.state.clock()


def utcnow() -> datetime:
    return datetime.now(UTC)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@contextmanager
def password_slot():
    """At most two 64 MiB password operations per worker process."""
    if not PASSWORD_SLOTS.acquire(timeout=1):
        raise HTTPException(
            503, "Authentication busy. Try again later.", headers={"Retry-After": "1"}
        )
    try:
        yield
    finally:
        PASSWORD_SLOTS.release()


def hash_password(password: str) -> str:
    with password_slot():
        return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    with password_slot():
        try:
            return PASSWORD_HASHER.verify(encoded, password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def check_origin(request: Request) -> None:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "Cross-site request rejected.")
    expected = request.app.state.settings.app_origin
    origin = request.headers.get("origin")
    if origin is not None:
        if origin != expected:
            raise HTTPException(403, "Untrusted request origin.")
    elif request.headers.get("referer"):
        try:
            reference = urlsplit(request.headers["referer"])
        except ValueError:
            raise HTTPException(403, "Untrusted request origin.") from None
        if f"{reference.scheme}://{reference.netloc}" != expected:
            raise HTTPException(403, "Untrusted request origin.")


def find_session(request: Request, db: Session) -> AuthSession | None:
    token = request.cookies.get(request.app.state.settings.cookie_name, "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        return None
    row = db.scalar(select(AuthSession).where(AuthSession.token_digest == digest(token)))
    timestamp = now(request)
    settings = request.app.state.settings
    if (
        row is None
        or row.revoked_at is not None
        or row.expires_at <= timestamp
        or (
            row.user_id is not None
            and row.last_seen_at + timedelta(seconds=settings.session_idle_seconds) <= timestamp
        )
    ):
        return None
    return row


def new_session(request: Request, response: Response, db: Session, user_id=None) -> AuthSession:
    settings = request.app.state.settings
    timestamp = now(request)
    lifetime = settings.session_absolute_seconds if user_id else settings.anonymous_session_seconds
    token = secrets.token_urlsafe(32)
    row = AuthSession(
        token_digest=digest(token),
        csrf_token=secrets.token_urlsafe(32),
        user_id=user_id,
        created_at=timestamp,
        last_seen_at=timestamp,
        expires_at=timestamp + timedelta(seconds=lifetime),
    )
    db.add(row)
    db.flush()
    response.set_cookie(
        settings.cookie_name,
        token,
        max_age=lifetime,
        path="/",
        secure=not settings.allow_insecure_local_http,
        httponly=True,
        samesite="lax",
    )
    response.headers["Cache-Control"] = "no-store"
    return row


def require_csrf(request: Request, row: AuthSession) -> None:
    check_origin(request)
    supplied = request.headers.get("x-csrf-token", "")
    if not secrets.compare_digest(supplied.encode(), row.csrf_token.encode()):
        raise HTTPException(403, "Missing or invalid CSRF token.")


def csrf_session(request: Request, db: DatabaseSession, csrf_header: CsrfHeader) -> AuthSession:
    row = find_session(request, db)
    if row is None:
        raise HTTPException(403, "Obtain a CSRF token before this request.")
    require_csrf(request, row)
    return row


def current_user(request: Request, db: DatabaseSession, csrf_header: CsrfHeader) -> User:
    row = find_session(request, db)
    if row is None or row.user_id is None:
        raise HTTPException(401, "Authentication required.")
    # All authenticated requests lock user first, then session. Logout-all/login
    # use the same order so revocation cannot race a new authenticated write.
    # FOR NO KEY UPDATE serializes this user's requests while allowing FK
    # key-share locks (e.g. another owner's membership insert) without deadlocks.
    user = db.scalar(select(User).where(User.id == row.user_id).with_for_update(key_share=True))
    db.refresh(row, with_for_update=True)
    timestamp = now(request)
    if (
        user is None
        or not user.is_active
        or row.revoked_at is not None
        or row.expires_at <= timestamp
        or row.last_seen_at + timedelta(seconds=request.app.state.settings.session_idle_seconds)
        <= timestamp
    ):
        raise HTTPException(401, "Authentication required.")
    if request.method in UNSAFE_METHODS:
        require_csrf(request, row)
    row.last_seen_at = timestamp
    request.state.auth_session = row
    return user


CurrentUser = Annotated[User, Depends(current_user)]
