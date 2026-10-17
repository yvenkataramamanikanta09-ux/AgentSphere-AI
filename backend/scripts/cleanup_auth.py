"""Remove expired/revoked sessions and expired counters; preserve all users/documents."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, or_
from sqlalchemy.orm import Session

from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.models import AuthSession, RateLimitBucket


def main():
    settings = Settings()
    if settings.database_url is None:
        raise SystemExit("DATABASE_URL is required.")
    timestamp = datetime.now(UTC)
    engine = build_engine(settings.database_url.get_secret_value())
    try:
        with Session(engine) as db, db.begin():
            sessions = db.execute(
                delete(AuthSession).where(
                    or_(
                        AuthSession.expires_at <= timestamp,
                        AuthSession.revoked_at.is_not(None),
                        (
                            AuthSession.user_id.is_not(None)
                            & (
                                AuthSession.last_seen_at
                                <= timestamp - timedelta(seconds=settings.session_idle_seconds)
                            )
                        ),
                    )
                )
            ).rowcount
            buckets = db.execute(
                delete(RateLimitBucket).where(RateLimitBucket.expires_at <= timestamp)
            ).rowcount
        print(f"Removed {sessions} expired/revoked sessions and {buckets} expired rate buckets.")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
