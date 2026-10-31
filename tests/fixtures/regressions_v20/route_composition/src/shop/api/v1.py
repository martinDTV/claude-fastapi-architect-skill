"""Version router: own prefix /v1, mounted by the application under /api."""

from fastapi import APIRouter

from shop.api.items import router as items_router
from shop.api.orders import router as orders_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(items_router, prefix="/items")
api_router.include_router(orders_router)
