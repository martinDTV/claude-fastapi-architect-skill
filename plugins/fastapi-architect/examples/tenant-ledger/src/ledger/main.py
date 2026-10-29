from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ledger.api.router import api_router
from ledger.core.config import Settings, get_settings
from ledger.core.db import build_engine, build_session_factory
from ledger.core.errors import install_error_handlers


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = build_engine(settings.database_url, echo=settings.echo_sql)
        app.state.engine = engine
        app.state.session_factory = build_session_factory(engine)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(title="tenant-ledger", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
