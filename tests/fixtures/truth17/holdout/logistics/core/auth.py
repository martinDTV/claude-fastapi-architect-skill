from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, Header, HTTPException


@dataclass
class Driver:
    id: UUID
    depot_id: UUID
    scopes: tuple


async def _bearer(authorization: str = Header(default="")) -> str:
    return authorization.removeprefix("Bearer ")


async def current_driver(token: str = Depends(_bearer)) -> Driver:
    if not token:
        raise HTTPException(401)
    return Driver(id=UUID(int=3), depot_id=UUID(int=11), scopes=("shipments:read",))


async def optional_driver(token: str = Depends(_bearer)) -> Driver | None:
    """Anonymous callers are allowed: the route decides what they may see."""
    return None if not token else Driver(id=UUID(int=3), depot_id=UUID(int=11), scopes=())


class Scope:
    def __init__(self, scope: str) -> None:
        self.scope = scope

    async def __call__(self, driver: Driver = Depends(current_driver)) -> Driver:
        if self.scope not in driver.scopes:
            raise HTTPException(403)
        return driver


async def depot_member(depot_id: UUID, driver: Driver = Depends(current_driver)) -> Driver:
    if driver.depot_id != depot_id:
        raise HTTPException(404)
    return driver


async def load_shipment(shipment_id: UUID, driver: Driver = Depends(current_driver)) -> dict:
    """Resource loader as a dependency: the shipment must belong to the driver's depot."""
    if shipment_id.int % 3 == 0:
        raise HTTPException(404)
    return {"id": shipment_id, "depot_id": driver.depot_id}
