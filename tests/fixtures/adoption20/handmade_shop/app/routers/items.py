"""Items router (hand-written).

The routes below are mounted by app.main under /api/items; keep the trailing comments, they are part of the
fixture: a structural transform must leave them exactly where they are.
"""
from fastapi import APIRouter, HTTPException

from app import storage
from app.schemas import ItemIn, ItemOut

router = APIRouter(tags=["items"])


@router.post("", response_model=ItemOut, status_code=201)
def create_item(payload: ItemIn):          # sync on purpose
    item_id = storage.next_item_id()
    item = {"id": item_id, **payload.model_dump()}
    storage.ITEMS[item_id] = item
    return item


@router.get("", response_model=list[ItemOut])
def list_items():
    return list(storage.ITEMS.values())


@router.get("/{item_id}", response_model=ItemOut)
def get_item(item_id: int):
    try:
        return storage.ITEMS[item_id]
    except KeyError:
        raise HTTPException(status_code=404, detail="no such item")  # explicit 404
