"""Versioned API router: every business router is mounted under /api/v1 behind the API key."""

from fastapi import APIRouter, Depends

from parcels.api.shipments import router as shipments_router
from parcels.core.security import require_api_key

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
api_router.include_router(shipments_router)
