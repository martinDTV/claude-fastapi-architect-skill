"""HTTP boundary of the catalog module: schema validation, status codes, problem details for domain errors."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from inventory_service.dependencies.catalog import ProductServiceDep
from inventory_service.schemas.catalog import ProductCreate, ProductRead, ProductUpdate

router = APIRouter(tags=["catalog"])


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate, service: ProductServiceDep) -> ProductRead:
    return ProductRead.model_validate(await service.create(payload))


@router.get("/products")
async def list_products(
    service: ProductServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProductRead]:
    return [ProductRead.model_validate(item) for item in await service.list(limit, offset)]


@router.get("/products/{product_id}")
async def get_product(product_id: UUID, service: ProductServiceDep) -> ProductRead:
    return ProductRead.model_validate(await service.get(product_id))


@router.patch("/products/{product_id}")
async def update_product(product_id: UUID, payload: ProductUpdate, service: ProductServiceDep) -> ProductRead:
    return ProductRead.model_validate(await service.update(product_id, payload))


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(product_id: UUID, service: ProductServiceDep) -> None:
    await service.delete(product_id)
