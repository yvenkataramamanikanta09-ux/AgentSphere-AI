"""Assemble the application; keep endpoint behavior in the API modules."""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import sessionmaker

from agentsphere.api import auth, document_records, documents, health, workspaces
from agentsphere.config import Settings
from agentsphere.database import build_engine
from agentsphere.middleware import SecurityMiddleware
from agentsphere.security import utcnow


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
        version="0.3.0",
        description="Validate and persist document metadata. No document uploads or AI processing.",
        lifespan=lifespan,
    )
    application.state.settings = configuration
    application.state.session_factory = None
    application.state.clock = utcnow
    application.add_middleware(SecurityMiddleware)

    @application.exception_handler(RequestValidationError)
    async def safe_validation(request: Request, error: RequestValidationError):
        if request.url.path.startswith("/api/v1/auth/"):
            return JSONResponse(
                status_code=422,
                content={
                    "detail": [
                        {
                            "loc": item["loc"],
                            "type": item["type"],
                            "msg": "Invalid authentication input.",
                        }
                        for item in error.errors()
                    ]
                },
            )
        return await request_validation_exception_handler(request, error)

    application.include_router(health.router)
    application.include_router(documents.router)
    application.include_router(auth.router)
    application.include_router(workspaces.router)
    application.include_router(document_records.router)
    return application
