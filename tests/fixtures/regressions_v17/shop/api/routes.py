"""Regression shapes for the v1.7 review: health-convention paths versus declared contracts, and one
question per route when tenant, resource and privilege ids meet."""

from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

app = FastAPI()
router = APIRouter(prefix="/api")


async def current_user() -> dict:
    return {"id": 1}


async def get_db() -> AsyncSession:
    raise NotImplementedError


async def reroute_service(db: AsyncSession, shipment_id: UUID, depot_id: UUID) -> None:
    await db.execute("update shipments set depot_id = :d where id = :id")


async def set_role_service(db: AsyncSession, user_id: UUID, role: str) -> None:
    await db.execute("update users set role = :r where id = :id")


@app.get("/health")
async def health() -> dict:          # convention: no finding, exposure undetermined, not declared public
    return {"ok": True}


@router.get("/status")
async def status() -> dict:          # convention under a protected prefix: the declared contract still applies
    return {"ok": True}


@router.get("/public/prices")
async def prices() -> dict:          # declared public: no contract violation
    return {}


@router.get("/orders")
async def orders() -> list:          # neither convention nor declaration: unauthenticated and contract violation
    return []


@router.post("/shipments/{shipment_id}/reroute")
async def reroute(shipment_id: UUID, depot_id: UUID, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await reroute_service(db, shipment_id, depot_id)   # tenant question only; the resource question is not repeated
    return {}


@router.post("/users/{user_id}/role")
async def set_role(user_id: UUID, role: str, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await set_role_service(db, user_id, role)          # privilege question only
    return {}


@router.get("/shipments/{shipment_id}")
async def shipment(shipment_id: UUID, user: dict = Depends(current_user), db: AsyncSession = Depends(get_db)) -> dict:
    await reroute_service(db, shipment_id, shipment_id)   # resource question alone (no tenant param on the route)
    return {}
