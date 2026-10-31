"""Application factory: settings, lifespan-owned resources, error handlers, routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from team_notes.api.router import api_router
from team_notes.core.config import Settings, get_settings
from team_notes.core.errors import install_error_handlers
from team_notes.core.security import JwksSource
from team_notes.repositories.notes import InMemoryNoteRepository, InMemoryTagRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.repositories = {}
        app.state.repositories["note"] = InMemoryNoteRepository()
        app.state.repositories["tag"] = InMemoryTagRepository()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.jwks = JwksSource(settings)  # fails at startup when the JWKS source is misconfigured
    install_error_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
