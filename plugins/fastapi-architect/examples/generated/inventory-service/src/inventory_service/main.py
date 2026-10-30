"""Application factory: settings, lifespan-owned resources, error handlers, routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from inventory_service.api.router import api_router
from inventory_service.core.config import Settings, get_settings
from inventory_service.core.errors import install_error_handlers
from inventory_service.repositories.catalog import InMemoryProductRepository


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.repositories = {}
        app.state.repositories["product"] = InMemoryProductRepository()
        yield

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
