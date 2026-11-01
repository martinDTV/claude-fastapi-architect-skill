"""Composition of the module: repositories, ports and services as FastAPI dependencies."""

from typing import Annotated

from fastapi import Depends

from parcels.core.db import SessionDep
from parcels.repositories.shipments import ShipmentRepository, SqlShipmentRepository
from parcels.services.shipments import ShipmentService


def get_shipment_repository(session: SessionDep) -> ShipmentRepository:
    return SqlShipmentRepository(session)


def get_shipment_service(
    repository: Annotated[ShipmentRepository, Depends(get_shipment_repository)],
    session: SessionDep,
) -> ShipmentService:
    return ShipmentService(repository, session)


ShipmentServiceDep = Annotated[ShipmentService, Depends(get_shipment_service)]
