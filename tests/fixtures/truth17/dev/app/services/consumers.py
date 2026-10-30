"""Event consumers: at-least-once delivery needs a redelivery guard (inbox) or an idempotent handler."""

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def handle_payment_event(session: AsyncSession, message_id: str, payload: dict) -> None:  # truth: REL_CONSUMER_WITHOUT_INBOX evidence=unknown
    await session.execute(text("insert into payments (amount) values (:a)"), {"a": payload["amount"]})
    await session.commit()


async def handle_payment_event_once(session: AsyncSession, message_id: str, payload: dict) -> None:  # truth-absent: REL_CONSUMER_WITHOUT_INBOX
    claimed = await session.execute(text("insert into processed_messages (id) values (:i) on conflict do nothing returning id"), {"i": message_id})
    if claimed.scalar_one_or_none() is None:
        return
    await session.execute(text("insert into payments (amount) values (:a)"), {"a": payload["amount"]})
    await session.commit()


async def handle_ping(message: dict) -> dict:                                          # truth-absent: REL_CONSUMER_WITHOUT_INBOX
    return {"pong": message.get("id")}                                                 # no write: nothing to deduplicate
