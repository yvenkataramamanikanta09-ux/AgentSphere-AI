"""Connection pool per app, session per request, no automatic schema creation."""

import logging
from collections.abc import Iterator

from fastapi import HTTPException, Request
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def build_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        pool_timeout=5,
        connect_args={
            "connect_timeout": 3,
            "options": "-c timezone=UTC -c statement_timeout=10000",
        },
        hide_parameters=True,
    )


def get_session(request: Request) -> Iterator[Session]:
    factory = request.app.state.session_factory
    if factory is None:
        raise HTTPException(status_code=503, detail="Database is not configured.")
    with factory() as session:
        try:
            yield session
        except SQLAlchemyError as error:
            session.rollback()
            # Do not expose connection URLs, SQL parameters, or driver exception text.
            logger.warning("Database operation failed (%s)", type(error).__name__)
            raise HTTPException(
                status_code=503, detail="Database operation unavailable. Please try again later."
            ) from None
