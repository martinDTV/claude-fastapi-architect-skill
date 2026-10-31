"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from parcels.models.shipments import Shipment


class ShipmentRepository(Protocol):
    async def add(self, shipment: Shipment) -> Shipment: ...

    async def get(self, shipment_id: UUID) -> Shipment | None: ...

    async def list(self, limit: int, offset: int) -> list[Shipment]: ...

    async def save(self, shipment: Shipment) -> Shipment: ...


class SqlShipmentRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, shipment: Shipment) -> Shipment:
        self._session.add(shipment)
        return shipment

    async def get(self, shipment_id: UUID) -> Shipment | None:
        statement = select(Shipment).where(Shipment.id == shipment_id)
        return await self._session.scalar(statement)

    async def list(self, limit: int, offset: int) -> list[Shipment]:
        statement = select(Shipment).order_by(Shipment.created_at, Shipment.id)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)

    async def save(self, shipment: Shipment) -> Shipment:
        return shipment  # tracked by the session; written at commit
