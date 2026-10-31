"""Own prefix /orders, mounted below /api/v1: effective /api/v1/orders/{order_id}, not declared public."""

from fastapi import APIRouter

router = APIRouter(prefix="/orders", tags=["orders"])


@router.get("/{order_id}")
async def get_order(order_id: int) -> dict:
    return {"id": order_id}
