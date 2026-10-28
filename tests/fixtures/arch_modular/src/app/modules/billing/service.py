from fastapi import HTTPException                     # service bound to the web framework

from app.modules.billing.repository import InvoiceRepository


def price_for(item_id: int) -> int:
    if item_id < 0:
        raise HTTPException(status_code=400)
    return 1
