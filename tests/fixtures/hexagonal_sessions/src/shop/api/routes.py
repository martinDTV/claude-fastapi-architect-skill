from typing import Annotated

from fastapi import APIRouter, Depends

from shop.adapters.sqlalchemy_uow import SqlUnitOfWork
from shop.application.place_order import place_order
from shop.domain.ports import UnitOfWork


def get_uow() -> UnitOfWork:  # wired in the app factory in real code
    raise NotImplementedError


def current_user():
    return {"id": 1}


router = APIRouter(dependencies=[Depends(current_user)])
UowDep = Annotated[UnitOfWork, Depends(get_uow)]


@router.post("/orders/{order_id}")
async def post_order(order_id: int, total: int, uow: UowDep) -> dict:
    order = await place_order(uow, order_id, total)
    return {"id": order.id}
