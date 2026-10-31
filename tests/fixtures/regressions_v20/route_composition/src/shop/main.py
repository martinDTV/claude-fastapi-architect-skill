from fastapi import FastAPI

from shop.api.dynamic import router as dynamic_router
from shop.api.reports import router as reports_router
from shop.api.v1 import api_router
from shop.settings import settings

app = FastAPI()
app.include_router(api_router, prefix="/api")
app.include_router(reports_router, prefix="/internal")
app.include_router(reports_router, prefix="/partner")
app.include_router(dynamic_router, prefix=settings.prefix)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
