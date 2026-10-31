"""HTTP boundary of the orders module: schema validation, status codes, problem details for domain errors."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from orders_platform.core.security import TenantPrincipalDep
from orders_platform.modules.orders.dependencies import OrderServiceDep
from orders_platform.modules.orders.schemas import OrderCreate, OrderRead

router = APIRouter(tags=["orders"])


@router.post("/tenants/{tenant_id}/orders", status_code=status.HTTP_201_CREATED)
async def create_order(principal: TenantPrincipalDep, payload: OrderCreate, service: OrderServiceDep) -> OrderRead:
    return OrderRead.model_validate(await service.create(principal.tenant_id, payload))


@router.get("/tenants/{tenant_id}/orders")
async def list_orders(
    principal: TenantPrincipalDep,
    service: OrderServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[OrderRead]:
    return [OrderRead.model_validate(item) for item in await service.list(principal.tenant_id, limit, offset)]


@router.get("/tenants/{tenant_id}/orders/{order_id}")
async def get_order(principal: TenantPrincipalDep, order_id: UUID, service: OrderServiceDep) -> OrderRead:
    return OrderRead.model_validate(await service.get(principal.tenant_id, order_id))
