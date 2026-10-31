"""Application entry point: two routers mounted under /api."""
from fastapi import FastAPI

from app.routers.items import router as items_router
from app.routers.users import router as users_router

app = FastAPI(title="handmade shop")
app.include_router(items_router, prefix="/api/items")
app.include_router(users_router, prefix="/api/users")


@app.get("/health")
def health():
    return {"ok": True}
