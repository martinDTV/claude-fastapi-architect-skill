import httpx

from app.db import SessionLocal
from app.models import Order


def notify_sync(session, order_id: int) -> None:
    session.add(Order(id=order_id))          # receives the request session


async def send_email(order_id: int) -> None:
    async with httpx.AsyncClient() as client:
        await client.post("https://mail.invalid/send", json={"order": order_id})


async def send_receipt(user, order_id: int) -> None:
    async with httpx.AsyncClient() as client:
        await client.post("https://mail.invalid/receipt", json={"user": user, "order": order_id})


async def reindex(order_id: int) -> None:
    async with SessionLocal() as session:    # recreates its own session
        await session.execute("select set_config('app.tenant_id', 'x', true)")
        session.add(Order(id=order_id))
        await session.commit()


async def audit_log(order_id: int) -> None:
    async with SessionLocal() as session:
        session.add(Order(id=order_id))
        await session.commit()
