"""Hand-written services: the extension points an adopted router may delegate to."""

from app import storage


def count_items(min_price: float = 0.0) -> dict:
    """How many items cost at least ``min_price``."""
    return {"count": sum(1 for item in storage.ITEMS.values() if item["price"] >= min_price), "min_price": min_price}


async def catalogue_value() -> dict:
    """Total value of the catalogue (async on purpose: the extension must await it)."""
    return {"total": round(sum(item["price"] for item in storage.ITEMS.values()), 2)}
