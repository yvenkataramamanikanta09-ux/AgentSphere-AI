"""Shared fixed-window throttling; counters commit even when login fails."""

import hashlib
import hmac
from datetime import UTC, datetime

from fastapi import HTTPException, Request
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool

from agentsphere.models import RateLimitBucket
from agentsphere.security import now

EMAIL = TypeAdapter(EmailStr)


def consume(request: Request, rules: list[tuple[str, str, int, int]]) -> None:
    settings = request.app.state.settings
    factory = request.app.state.session_factory
    if factory is None or settings.auth_rate_limit_key is None:
        raise HTTPException(503, "Authentication is not configured.")
    timestamp = int(now(request).timestamp())
    retry = 0
    try:
        with factory() as db, db.begin():
            for scope, identity, seconds, limit in sorted(rules):
                start = timestamp // seconds * seconds
                key = hmac.new(
                    settings.auth_rate_limit_key.get_secret_value().encode(),
                    f"{scope}:{identity}".encode(),
                    hashlib.sha256,
                ).hexdigest()
                statement = (
                    insert(RateLimitBucket)
                    .values(
                        scope=scope,
                        identity_digest=key,
                        window_start=start,
                        count=1,
                        expires_at=datetime.fromtimestamp(start + seconds, UTC),
                    )
                    .on_conflict_do_update(
                        index_elements=["scope", "identity_digest", "window_start"],
                        set_={"count": RateLimitBucket.count + 1},
                    )
                    .returning(RateLimitBucket.count)
                )
                if db.scalar(statement) > limit:
                    retry = max(retry, start + seconds - timestamp)
    except SQLAlchemyError:
        raise HTTPException(503, "Authentication temporarily unavailable.") from None
    if retry:
        raise HTTPException(
            429, "Too many attempts. Try again later.", headers={"Retry-After": str(retry)}
        )


async def auth_rate_limit(request: Request) -> None:
    settings = request.app.state.settings
    ip = request.client.host if request.client else "unknown"
    endpoint = request.url.path.rsplit("/", 1)[-1]
    if endpoint == "csrf":
        rules = [("csrf_ip", ip, 60, settings.csrf_ip_limit)]
    elif endpoint == "register":
        rules = [("register_ip", ip, 3600, settings.registration_ip_limit)]
    else:
        try:
            body = await request.json()
        except ValueError:
            body = {}
        raw_email = body.get("email", "") if isinstance(body, dict) else ""
        email = raw_email.strip().casefold() if isinstance(raw_email, str) else ""
        try:
            # Same normalization as login, including display-name/IDN forms.
            email = EMAIL.validate_python(email).casefold()
        except ValidationError:
            email = email[:254]
        rules = [
            ("login_ip", ip, 300, settings.login_ip_limit),
            ("login_account", email, 900, settings.login_account_limit),
        ]
    await run_in_threadpool(consume, request, rules)
