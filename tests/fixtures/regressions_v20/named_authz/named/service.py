"""A service that receives resource ids and never a principal."""

from uuid import UUID


async def update(item_id: UUID, payload: dict) -> dict:
    return {"id": str(item_id), **payload}


async def read(item_id: UUID) -> dict:
    return {"id": str(item_id)}
