"""Versioned API router: every business router is mounted under /api/v1 behind the API key."""

from fastapi import APIRouter, Depends

from orders_platform.core.security import require_api_key
from orders_platform.modules.billing.router import router as billing_router
from orders_platform.modules.customers.router import router as customers_router
from orders_platform.modules.orders.router import router as orders_router

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
api_router.include_router(customers_router)
api_router.include_router(orders_router)
api_router.include_router(billing_router)
