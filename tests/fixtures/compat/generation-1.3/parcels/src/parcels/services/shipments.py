"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from parcels.core.errors import ConflictError, NotFoundError
from parcels.models.shipments import Shipment
from parcels.repositories.shipments import ShipmentRepository
from parcels.schemas.shipments import ShipmentCreate, ShipmentUpdate


class ShipmentService:
    def __init__(self, repository: ShipmentRepository, session: AsyncSession) -> None:
        self._repository = repository
        self._session = session

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Shipment violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, data: ShipmentCreate) -> Shipment:
        shipment = Shipment(reference=data.reference, weight_kg=data.weight_kg)
        await self._repository.add(shipment)
        await self._commit()
        return shipment

    async def get(self, shipment_id: UUID) -> Shipment:
        shipment = await self._repository.get(shipment_id)
        if shipment is None:
            raise NotFoundError(f"Shipment {shipment_id} not found")
        return shipment

    async def list(self, limit: int = 50, offset: int = 0) -> list[Shipment]:
        return await self._repository.list(limit, offset)

    async def update(self, shipment_id: UUID, data: ShipmentUpdate) -> Shipment:
        shipment = await self.get(shipment_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(shipment, name, value)
        await self._repository.save(shipment)
        await self._commit()
        return shipment
