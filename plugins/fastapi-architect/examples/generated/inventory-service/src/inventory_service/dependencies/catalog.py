"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends, Request

from inventory_service.repositories.catalog import ProductRepository
from inventory_service.services.catalog import ProductService


def get_product_repository(request: Request) -> ProductRepository:
    return request.app.state.repositories["product"]


def get_product_service(repository: Annotated[ProductRepository, Depends(get_product_repository)]) -> ProductService:
    return ProductService(repository)


ProductServiceDep = Annotated[ProductService, Depends(get_product_service)]
