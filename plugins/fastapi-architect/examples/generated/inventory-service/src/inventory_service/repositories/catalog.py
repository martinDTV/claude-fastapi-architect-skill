"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from inventory_service.models.catalog import Product


class ProductRepository(Protocol):
    async def add(self, product: Product) -> Product: ...

    async def get(self, product_id: UUID) -> Product | None: ...

    async def list(self, limit: int, offset: int) -> list[Product]: ...

    async def save(self, product: Product) -> Product: ...

    async def delete(self, product_id: UUID) -> bool: ...


class InMemoryProductRepository:
    """Process-local store; the lifespan creates one per application, tests get a fresh one per app."""

    def __init__(self) -> None:
        self._items: dict[UUID, Product] = {}

    async def add(self, product: Product) -> Product:
        self._items[product.id] = product
        return product

    async def get(self, product_id: UUID) -> Product | None:
        return self._items.get(product_id)

    async def list(self, limit: int, offset: int) -> list[Product]:
        items = sorted(self._items.values(), key=lambda item: (item.created_at, str(item.id)))
        return items[offset : offset + limit]

    async def save(self, product: Product) -> Product:
        self._items[product.id] = product
        return product

    async def delete(self, product_id: UUID) -> bool:
        item = await self.get(product_id)
        if item is None:
            return False
        del self._items[product_id]
        return True
