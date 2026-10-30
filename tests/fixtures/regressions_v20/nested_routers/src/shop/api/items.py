"""Business router without its own dependencies: protected only through the routers that include it."""

from fastapi import APIRouter

router = APIRouter(tags=["items"])


@router.get("/items/{item_id}")
async def get_item(item_id: int) -> dict:
    return {"id": item_id}


@router.post("/items", status_code=201)
async def create_item(payload: dict) -> dict:
    return payload
