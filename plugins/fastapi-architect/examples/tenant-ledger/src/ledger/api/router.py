from fastapi import APIRouter

from ledger.modules.ledger.router import router as ledger_router
from ledger.modules.tenants.router import router as tenants_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(tenants_router)
api_router.include_router(ledger_router)


@api_router.get("/health", tags=["ops"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
