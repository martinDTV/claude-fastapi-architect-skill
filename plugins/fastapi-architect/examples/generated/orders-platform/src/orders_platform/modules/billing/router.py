"""HTTP boundary of the billing module: schema validation, status codes, problem details for domain errors."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from orders_platform.core.security import TenantPrincipalDep
from orders_platform.modules.billing.dependencies import InvoiceServiceDep
from orders_platform.modules.billing.schemas import InvoiceCreate, InvoiceRead

router = APIRouter(tags=["billing"])


@router.post("/tenants/{tenant_id}/invoices", status_code=status.HTTP_201_CREATED)
async def create_invoice(
    principal: TenantPrincipalDep,
    payload: InvoiceCreate,
    service: InvoiceServiceDep,
) -> InvoiceRead:
    return InvoiceRead.model_validate(await service.create(principal.tenant_id, payload))


@router.get("/tenants/{tenant_id}/invoices")
async def list_invoices(
    principal: TenantPrincipalDep,
    service: InvoiceServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[InvoiceRead]:
    return [InvoiceRead.model_validate(item) for item in await service.list(principal.tenant_id, limit, offset)]


@router.get("/tenants/{tenant_id}/invoices/{invoice_id}")
async def get_invoice(principal: TenantPrincipalDep, invoice_id: UUID, service: InvoiceServiceDep) -> InvoiceRead:
    return InvoiceRead.model_validate(await service.get(principal.tenant_id, invoice_id))
