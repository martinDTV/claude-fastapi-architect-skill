"""Retries, lost responses, outbox and inbox, concurrent transactions."""

import asyncio
from uuid import UUID

import backoff
import httpx
import requests
import tenacity
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

producer = None  # bound at startup to a broker producer


class Account:
    id: UUID
    balance: int


@tenacity.retry(stop=tenacity.stop_after_attempt(3))                                   # truth-absent: RETRY_UNBOUNDED
async def charge(client: httpx.AsyncClient, amount: int) -> None:
    await client.post("/charges", json={"amount": amount})                             # truth: RETRY_NON_IDEMPOTENT_WRITE


@tenacity.retry(stop=tenacity.stop_after_attempt(3))
async def charge_with_key(client: httpx.AsyncClient, amount: int, idempotency_key: str) -> None:
    await client.post("/charges", json={"amount": amount}, headers={"Idempotency-Key": idempotency_key})  # truth-absent: RETRY_NON_IDEMPOTENT_WRITE


@tenacity.retry()                                                                      # truth: RETRY_UNBOUNDED
async def ping(client: httpx.AsyncClient) -> None:
    await client.get("/health")


@backoff.on_exception(backoff.expo, Exception, max_tries=5)                            # truth-absent: RETRY_UNBOUNDED
async def ping_bounded(client: httpx.AsyncClient) -> None:
    await client.get("/health")


async def lost_response_reconciled(client: httpx.AsyncClient, amount: int, idempotency_key: str) -> dict:
    """After a timeout the outcome is unknown: look the operation up by key before retrying."""
    try:
        return (await client.post("/charges", json={"amount": amount}, headers={"Idempotency-Key": idempotency_key})).json()
    except httpx.TimeoutException:
        found = await client.get(f"/charges/by-key/{idempotency_key}")
        if found.status_code == 200:
            return found.json()
        raise


def fetch_rates() -> dict:
    return requests.get("https://rates.invalid/latest").json()                        # truth: REL_HTTP_CALL_WITHOUT_TIMEOUT


def fetch_rates_bounded() -> dict:
    return requests.get("https://rates.invalid/latest", timeout=(3, 10)).json()       # truth-absent: REL_HTTP_CALL_WITHOUT_TIMEOUT


async def publish_direct(session: AsyncSession, account: Account) -> None:
    session.add(account)
    await session.commit()
    await producer.send_and_wait("accounts", b"created")                              # truth: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def publish_via_outbox(session: AsyncSession, account: Account) -> None:
    session.add(account)
    await session.execute(text("insert into outbox (topic, payload) values ('accounts', 'created')"))
    await session.commit()                                                             # truth-absent: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def consume(session: AsyncSession, message_id: str, payload: dict) -> None:
    """Inbox: the message id is recorded first; a redelivery hits the unique key and stops."""
    inserted = await session.execute(
        text("insert into inbox (message_id) values (:m) on conflict do nothing returning message_id"), {"m": message_id}
    )
    if inserted.scalar_one_or_none() is None:
        return
    await session.execute(text("update accounts set balance = balance + :d where id = :id"), {"d": payload["amount"], "id": payload["account_id"]})
    await session.commit()


async def naive_deposit(session: AsyncSession, account_id: UUID, amount: int) -> int:
    account = await session.get(Account, account_id)
    account.balance += amount                                                          # truth: TX_READ_MODIFY_WRITE
    await session.commit()
    return account.balance


async def atomic_deposit(session: AsyncSession, account_id: UUID, amount: int) -> None:
    await session.execute(text("update accounts set balance = balance + :d where id = :id"), {"d": amount, "id": account_id})  # truth-absent: TX_READ_MODIFY_WRITE
    await session.commit()


async def locked_deposit(session: AsyncSession, account_id: UUID, amount: int) -> int:
    account = (await session.execute(select(Account).where(Account.id == account_id).with_for_update())).scalar_one()
    account.balance += amount                                                          # truth-absent: TX_READ_MODIFY_WRITE
    await session.commit()
    return account.balance


async def notify_inside_tx(session: AsyncSession, client: httpx.AsyncClient, account_id: UUID) -> None:
    async with session.begin():
        await session.execute(text("update accounts set notified = true where id = :id"), {"id": account_id})
        await client.post("/notify", json={"id": str(account_id)})                      # truth: TX_EXTERNAL_CALL_IN_TRANSACTION


async def retry_loop_write(session: AsyncSession, account: Account) -> None:
    for _attempt in range(3):                                                          # truth: RETRY_NON_IDEMPOTENT_WRITE
        try:
            session.add(account)
            await session.commit()
            break
        except Exception:
            await asyncio.sleep(0)
