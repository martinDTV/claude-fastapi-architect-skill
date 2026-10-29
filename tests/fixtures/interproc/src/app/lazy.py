from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, selectinload

from app.models import Order


async def total(session: AsyncSession, oid: int) -> int:
    order = await session.get(Order, oid)
    return sum(item.price for item in order.items)           # implicit lazy load under AsyncSession


async def eager_default(session: AsyncSession, oid: int) -> str:
    order = await session.get(Order, oid)
    return order.customer.name                               # lazy="selectin": loaded with the row


async def guarded(session: AsyncSession, oid: int):
    order = await session.get(Order, oid)
    return order.audit                                       # lazy="raise": explicit, not implicit


async def with_options(session: AsyncSession, oid: int) -> int:
    result = await session.execute(select(Order).options(selectinload(Order.items)).where(Order.id == oid))
    order = result.scalar_one()
    return sum(item.price for item in order.items)           # loader option present: fine


async def from_result(session: AsyncSession) -> list:
    result = await session.execute(select(Order))
    out = []
    for order in result.scalars():
        out.append(len(order.items))                         # implicit lazy load per row
    return out


def sync_total(session: Session, oid: int) -> int:
    order = session.get(Order, oid)
    return sum(item.price for item in order.items)           # sync Session: N+1 at most, not MissingGreenlet
