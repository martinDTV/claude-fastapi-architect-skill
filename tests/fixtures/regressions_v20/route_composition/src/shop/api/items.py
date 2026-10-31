"""Mounted at /api/v1/items through two levels of include_router (v1 adds /items, the app adds /api)."""

from fastapi import APIRouter

router = APIRouter(tags=["items"])


@router.get("")
async def list_items() -> list:
    return []


@router.get("/{item_id}")
async def get_item(item_id: int) -> dict:
    return {"id": item_id}
