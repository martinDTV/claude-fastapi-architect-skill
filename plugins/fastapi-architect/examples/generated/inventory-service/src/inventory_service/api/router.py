"""Versioned API router: every business router is mounted under /api/v1 behind the API key."""

from fastapi import APIRouter, Depends

from inventory_service.api.catalog import router as catalog_router
from inventory_service.core.security import require_api_key

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
api_router.include_router(catalog_router)
