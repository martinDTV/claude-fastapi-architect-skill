"""HTTP boundary of the customers module: schema validation, status codes, problem details for domain errors."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from orders_platform.core.security import TenantPrincipalDep
from orders_platform.modules.customers.dependencies import CustomerServiceDep
from orders_platform.modules.customers.schemas import CustomerCreate, CustomerRead, CustomerUpdate

router = APIRouter(tags=["customers"])


@router.post("/tenants/{tenant_id}/customers", status_code=status.HTTP_201_CREATED)
async def create_customer(
    principal: TenantPrincipalDep,
    payload: CustomerCreate,
    service: CustomerServiceDep,
) -> CustomerRead:
    return CustomerRead.model_validate(await service.create(principal.tenant_id, payload))


@router.get("/tenants/{tenant_id}/customers")
async def list_customers(
    principal: TenantPrincipalDep,
    service: CustomerServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CustomerRead]:
    return [CustomerRead.model_validate(item) for item in await service.list(principal.tenant_id, limit, offset)]


@router.get("/tenants/{tenant_id}/customers/{customer_id}")
async def get_customer(principal: TenantPrincipalDep, customer_id: UUID, service: CustomerServiceDep) -> CustomerRead:
    return CustomerRead.model_validate(await service.get(principal.tenant_id, customer_id))


@router.patch("/tenants/{tenant_id}/customers/{customer_id}")
async def update_customer(
    principal: TenantPrincipalDep,
    customer_id: UUID,
    payload: CustomerUpdate,
    service: CustomerServiceDep,
) -> CustomerRead:
    return CustomerRead.model_validate(await service.update(principal.tenant_id, customer_id, payload))


@router.delete("/tenants/{tenant_id}/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(principal: TenantPrincipalDep, customer_id: UUID, service: CustomerServiceDep) -> None:
    await service.delete(principal.tenant_id, customer_id)
