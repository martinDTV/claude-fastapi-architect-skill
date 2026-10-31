"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from inventory_service.core.errors import NotFoundError
from inventory_service.models.catalog import Product
from inventory_service.repositories.catalog import ProductRepository
from inventory_service.schemas.catalog import ProductCreate, ProductUpdate


class ProductService:
    def __init__(self, repository: ProductRepository) -> None:
        self._repository = repository

    async def create(self, data: ProductCreate) -> Product:
        product = Product(
            sku=data.sku,
            name=data.name,
            quantity=data.quantity,
            unit_price=data.unit_price,
            notes=data.notes,
        )
        await self._repository.add(product)
        return product

    async def get(self, product_id: UUID) -> Product:
        product = await self._repository.get(product_id)
        if product is None:
            raise NotFoundError(f"Product {product_id} not found")
        return product

    async def list(self, limit: int = 50, offset: int = 0) -> list[Product]:
        return await self._repository.list(limit, offset)

    async def update(self, product_id: UUID, data: ProductUpdate) -> Product:
        product = await self.get(product_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(product, name, value)
        await self._repository.save(product)
        return product

    async def delete(self, product_id: UUID) -> None:
        removed = await self._repository.delete(product_id)
        if not removed:
            raise NotFoundError(f"Product {product_id} not found")
