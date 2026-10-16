"""Assemble the application; keep endpoint behavior in the API modules."""

from fastapi import FastAPI

from agentsphere.api import documents, health
from agentsphere.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Accept explicit settings so tests never depend on a developer's .env."""
    application = FastAPI(
        title="AgentSphere AI",
        version="0.1.0",
        description="Module 1: validate declared document metadata. No file uploads or storage.",
    )
    application.state.settings = settings if settings is not None else Settings()
    application.include_router(health.router)
    application.include_router(documents.router)
    return application
