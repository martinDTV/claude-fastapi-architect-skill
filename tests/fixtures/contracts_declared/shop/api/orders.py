from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from shop.infra.mailer import send
from shop.services import orders

router = APIRouter(prefix="/api")
public = APIRouter()


async def current_user() -> dict:
    return {}


class Admin:
    async def __call__(self, user: dict = Depends(current_user)) -> dict:
        return user


async def get_db() -> AsyncSession:
    raise NotImplementedError


SessionDep = Annotated[AsyncSession, Depends(get_db)]


@router.get("/orders")
async def list_orders() -> list:
    return []


@router.post("/orders")
async def create_order(payload: dict, user: dict = Depends(current_user), session: SessionDep = None) -> dict:
    await orders.place(session, 1)
    return {}


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int, admin: dict = Depends(Admin()), session: SessionDep = None) -> dict:
    session.add(object())
    await session.commit()
    send("ops@example.invalid")
    return {}


@public.get("/public/catalog")
async def catalog() -> list:
    return []
