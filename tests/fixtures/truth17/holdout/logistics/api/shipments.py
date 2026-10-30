from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends

from ..core import Driver, Scope, SessionDep, current_driver, load_shipment, optional_driver
from ..infra.carrier import legacy_rates                                              # truth: CONTRACT_IMPORT_FORBIDDEN
from ..services import shipments

router = APIRouter(prefix="/api")
public = APIRouter()


@public.get("/public/tracking/{code}")
async def public_tracking(code: str) -> dict:                                         # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    return {"code": code}


@router.get("/shipments/{shipment_id}")
async def read(shipment_id: UUID, driver: Driver = Depends(current_driver), session: SessionDep = None) -> dict:  # truth: SEC_RESOURCE_AUTHZ_UNVERIFIED evidence=unknown
    return await shipments.get(session, shipment_id)


@router.get("/shipments/{shipment_id}/mine")
async def read_mine(shipment_id: UUID, driver: Driver = Depends(current_driver), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    return await shipments.get_for_driver(session, driver, shipment_id)


@router.get("/shipments/{shipment_id}/loaded")
async def read_loaded(shipment_id: UUID, shipment: dict = Depends(load_shipment), session: SessionDep = None) -> dict:  # truth-absent: SEC_RESOURCE_AUTHZ_UNVERIFIED
    return shipment


@router.get("/shipments/{shipment_id}/preview")
async def preview(shipment_id: UUID, driver: Driver | None = Depends(optional_driver), session: SessionDep = None) -> dict:  # truth-absent: SEC_ROUTE_WITHOUT_AUTH_SIGNAL
    if driver is None:
        return {"id": str(shipment_id), "public": True}
    return await shipments.get_for_driver(session, driver, shipment_id)


@router.post("/shipments/{shipment_id}/reroute")
async def reroute(shipment_id: UUID, depot_id: UUID, driver: Driver = Depends(current_driver), session: SessionDep = None) -> dict:  # truth: CONTRACT_MUTATION_AUTHZ_REQUIRED
    await shipments.reroute(session, shipment_id, depot_id)                           # truth: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL evidence=unknown
    return {}


@router.post("/shipments/{shipment_id}/reroute-scoped")
async def reroute_scoped(shipment_id: UUID, depot_id: UUID, driver: Driver = Depends(Scope("shipments:write")), session: SessionDep = None) -> dict:  # truth-absent: CONTRACT_MUTATION_AUTHZ_REQUIRED
    if depot_id != driver.depot_id:
        raise PermissionError
    await shipments.reroute(session, shipment_id, depot_id)                           # truth-absent: SEC_TENANT_NOT_BOUND_TO_PRINCIPAL
    return {}


@router.get("/rates")
async def rates(driver: Driver = Depends(current_driver)) -> dict:
    return legacy_rates()


@router.get("/status")
async def status() -> dict:                                                           # truth: CONTRACT_ROUTE_AUTH_REQUIRED
    return {"ok": True}                                                               # truth: SEC_ROUTE_WITHOUT_AUTH_SIGNAL


@router.post("/shipments/{shipment_id}/close")
async def close(shipment_id: UUID, driver: Driver = Depends(current_driver), session: SessionDep = None) -> dict:  # truth: CONTRACT_COMMIT_OUTSIDE_LAYER
    await session.execute(shipments.text("update shipments set closed = true where id = :id"), {"id": shipment_id})
    await session.commit()
    return {}
