"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.modules.billing.models import Invoice


class InvoiceRepository(Protocol):
    async def add(self, invoice: Invoice) -> Invoice: ...

    async def get(self, tenant_id: UUID, invoice_id: UUID) -> Invoice | None: ...

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Invoice]: ...


class SqlInvoiceRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invoice: Invoice) -> Invoice:
        self._session.add(invoice)
        return invoice

    async def get(self, tenant_id: UUID, invoice_id: UUID) -> Invoice | None:
        statement = select(Invoice).where(Invoice.id == invoice_id).where(Invoice.tenant_id == tenant_id)
        return await self._session.scalar(statement)

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Invoice]:
        statement = select(Invoice).where(Invoice.tenant_id == tenant_id).order_by(Invoice.created_at, Invoice.id)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)
