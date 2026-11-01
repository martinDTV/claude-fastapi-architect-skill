"""HTTP boundary of the shipments module: schema validation, status codes, problem details for domain errors."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from parcels.dependencies.shipments import ShipmentServiceDep
from parcels.schemas.shipments import ShipmentCreate, ShipmentRead, ShipmentUpdate

router = APIRouter(tags=["shipments"])


@router.post("/shipments", status_code=status.HTTP_201_CREATED)
async def create_shipment(payload: ShipmentCreate, service: ShipmentServiceDep) -> ShipmentRead:
    return ShipmentRead.model_validate(await service.create(payload))


@router.get("/shipments")
async def list_shipments(
    service: ShipmentServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ShipmentRead]:
    return [ShipmentRead.model_validate(item) for item in await service.list(limit, offset)]


@router.get("/shipments/{shipment_id}")
async def get_shipment(shipment_id: UUID, service: ShipmentServiceDep) -> ShipmentRead:
    return ShipmentRead.model_validate(await service.get(shipment_id))


@router.patch("/shipments/{shipment_id}")
async def update_shipment(shipment_id: UUID, payload: ShipmentUpdate, service: ShipmentServiceDep) -> ShipmentRead:
    return ShipmentRead.model_validate(await service.update(shipment_id, payload))
