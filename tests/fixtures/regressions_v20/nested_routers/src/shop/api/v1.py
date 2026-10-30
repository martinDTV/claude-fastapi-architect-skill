"""Intermediate router: itself unprotected, included into the protected api_router (see router.py)."""

from fastapi import APIRouter

from shop.api.items import router as items_router
from shop.api.tenants import router as tenants_router

v1_router = APIRouter(prefix="/v1")
v1_router.include_router(items_router)
v1_router.include_router(tenants_router)
