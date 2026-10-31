"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.modules.customers.models import Customer


class CustomerRepository(Protocol):
    async def add(self, customer: Customer) -> Customer: ...

    async def get(self, tenant_id: UUID, customer_id: UUID) -> Customer | None: ...

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Customer]: ...

    async def save(self, customer: Customer) -> Customer: ...

    async def delete(self, tenant_id: UUID, customer_id: UUID) -> bool: ...


class SqlCustomerRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, customer: Customer) -> Customer:
        self._session.add(customer)
        return customer

    async def get(self, tenant_id: UUID, customer_id: UUID) -> Customer | None:
        statement = select(Customer).where(Customer.id == customer_id).where(Customer.tenant_id == tenant_id)
        return await self._session.scalar(statement)

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Customer]:
        statement = select(Customer).where(Customer.tenant_id == tenant_id).order_by(Customer.created_at, Customer.id)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)

    async def save(self, customer: Customer) -> Customer:
        return customer  # tracked by the session; written at commit

    async def delete(self, tenant_id: UUID, customer_id: UUID) -> bool:
        item = await self.get(tenant_id, customer_id)
        if item is None:
            return False
        await self._session.delete(item)
        return True
