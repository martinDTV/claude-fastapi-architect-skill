"""Application factory: settings, lifespan-owned resources, error handlers, routers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from orders_platform.api.router import api_router
from orders_platform.core.config import Settings, get_settings
from orders_platform.core.db import make_engine
from orders_platform.core.errors import install_error_handlers
from orders_platform.modules.billing.adapters.notifications import RecordingNotificationsAdapter
from orders_platform.modules.orders.adapters.payments import HttpPaymentsAdapter


def create_app(settings: Settings | None = None, ports: dict[str, object] | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = make_engine(settings.database_url)
        app.state.session_factory = async_sessionmaker(app.state.engine, expire_on_commit=False)
        payments_adapter = HttpPaymentsAdapter(settings.payments_base_url, settings.payments_timeout_s)
        app.state.ports.setdefault("payments", payments_adapter)
        app.state.ports.setdefault("notifications", RecordingNotificationsAdapter())
        yield
        await app.state.engine.dispose()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.state.ports = dict(ports or {})
    install_error_handlers(app)
    app.include_router(api_router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
