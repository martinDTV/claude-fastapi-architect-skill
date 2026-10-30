"""Consumers with and without an inbox; publishers with and without an outbox."""

import backoff
import httpx
import tenacity
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

kafka = None  # producer bound at startup


async def on_shipment_event(session: AsyncSession, message_id: str, payload: dict) -> None:  # truth: REL_CONSUMER_WITHOUT_INBOX evidence=unknown
    """Applies the event with nothing that recognises a redelivery."""
    await session.execute(text("update shipments set status = :s where id = :id"), {"s": payload["status"], "id": payload["id"]})
    await session.commit()


async def on_shipment_event_deduplicated(session: AsyncSession, message_id: str, payload: dict) -> None:  # truth-absent: REL_CONSUMER_WITHOUT_INBOX
    claimed = await session.execute(text("insert into inbox (message_id) values (:m) on conflict do nothing returning message_id"), {"m": message_id})
    if claimed.scalar_one_or_none() is None:
        return
    await session.execute(text("update shipments set status = :s where id = :id"), {"s": payload["status"], "id": payload["id"]})
    await session.commit()


async def publish_status(session: AsyncSession, shipment_id: str, status: str) -> None:
    await session.execute(text("update shipments set status = :s where id = :id"), {"s": status, "id": shipment_id})
    await session.commit()
    await kafka.send_and_wait("shipments", status.encode())                           # truth: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def publish_status_outbox(session: AsyncSession, shipment_id: str, status: str) -> None:
    await session.execute(text("update shipments set status = :s where id = :id"), {"s": status, "id": shipment_id})
    await session.execute(text("insert into outbox (topic, payload) values ('shipments', :p)"), {"p": status})
    await session.commit()                                                             # truth-absent: REL_DUAL_WRITE_WITHOUT_OUTBOX


@tenacity.retry(stop=tenacity.stop_after_attempt(4), wait=tenacity.wait_exponential())
async def notify_carrier(http: httpx.AsyncClient, shipment_id: str) -> None:
    await http.post("/notify", json={"id": shipment_id})                               # truth: RETRY_NON_IDEMPOTENT_WRITE


@tenacity.retry(stop=tenacity.stop_after_attempt(4))
async def notify_carrier_keyed(http: httpx.AsyncClient, shipment_id: str, idempotency_key: str) -> None:
    await http.post("/notify", json={"id": shipment_id}, headers={"Idempotency-Key": idempotency_key})  # truth-absent: RETRY_NON_IDEMPOTENT_WRITE


@backoff.on_exception(backoff.expo, httpx.TransportError)                             # truth: RETRY_UNBOUNDED
async def poll_carrier(http: httpx.AsyncClient) -> dict:
    return (await http.get("/status")).json()
