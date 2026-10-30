"""Negative control: a router included into an unprotected parent stays unprotected."""

from fastapi import APIRouter

open_router = APIRouter(tags=["open"])


@open_router.get("/open/{tenant_id}/stats")
async def tenant_stats(tenant_id: str) -> dict:
    return {"tenant": tenant_id}


@open_router.get("/open/items/{item_id}")
async def open_item(item_id: int) -> dict:
    return {"id": item_id}


plain_router = APIRouter(prefix="/plain")
plain_router.include_router(open_router)
