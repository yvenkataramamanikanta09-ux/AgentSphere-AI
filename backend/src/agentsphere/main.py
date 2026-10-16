"""Assemble the application; keep endpoint behavior in the API modules."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.orm import sessionmaker

from agentsphere.api import document_records, documents, health
from agentsphere.config import Settings
from agentsphere.database import build_engine


def create_app(settings: Settings | None = None) -> FastAPI:
    """Accept explicit settings so tests never depend on a developer's .env."""
    configuration = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        engine = (
            build_engine(configuration.database_url.get_secret_value())
            if configuration.database_url is not None
            else None
        )
        application.state.session_factory = (
            sessionmaker(engine, expire_on_commit=False) if engine is not None else None
        )
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    application = FastAPI(
        title="AgentSphere AI",
        version="0.2.0",
        description="Validate and persist document metadata. No document uploads or AI processing.",
        lifespan=lifespan,
    )
    application.state.settings = configuration
    application.state.session_factory = None
    application.include_router(health.router)
    application.include_router(documents.router)
    application.include_router(document_records.router)
    return application
