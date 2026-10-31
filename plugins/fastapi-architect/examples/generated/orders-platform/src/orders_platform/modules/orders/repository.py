"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from orders_platform.modules.orders.models import Order


class OrderRepository(Protocol):
    async def add(self, order: Order) -> Order: ...

    async def get(self, tenant_id: UUID, order_id: UUID) -> Order | None: ...

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Order]: ...


class SqlOrderRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: Order) -> Order:
        self._session.add(order)
        return order

    async def get(self, tenant_id: UUID, order_id: UUID) -> Order | None:
        statement = select(Order).where(Order.id == order_id).where(Order.tenant_id == tenant_id)
        return await self._session.scalar(statement)

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Order]:
        statement = select(Order).where(Order.tenant_id == tenant_id).order_by(Order.created_at, Order.id)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)
