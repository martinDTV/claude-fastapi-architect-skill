"""Application entry point: an application factory that mounts the routers under /api."""
from fastapi import FastAPI

from app.routers.items import router as items_router
from app.routers.users import router as users_router


def create_app() -> FastAPI:
    application = FastAPI(title="workshop")   # the only FastAPI instance of the project
    application.include_router(items_router, prefix="/api/items")
    application.include_router(users_router, prefix="/api/users")

    @application.get("/health")
    def health():
        return {"ok": True}

    return application


app = create_app()
