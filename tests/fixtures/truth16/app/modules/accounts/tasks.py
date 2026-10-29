import asyncio

import httpx
import tenacity
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Account, Outbox

producer = None  # e.g. aiokafka.AIOKafkaProducer, bound at startup
background: set = set()


async def work() -> None:
    await asyncio.sleep(0)


async def fire_and_forget() -> None:
    asyncio.create_task(work())                                                        # truth: ASYNC_TASK_REFERENCE_DROPPED


async def keep_reference() -> None:
    task = asyncio.create_task(work())                                                 # truth-absent: ASYNC_TASK_REFERENCE_DROPPED
    background.add(task)
    task.add_done_callback(background.discard)


async def await_it() -> None:
    await asyncio.create_task(work())                                                  # truth-absent: ASYNC_TASK_REFERENCE_DROPPED


@tenacity.retry()                                                                      # truth: RETRY_UNBOUNDED
async def unbounded(client: httpx.AsyncClient) -> None:
    await client.get("/health")


@tenacity.retry(stop=tenacity.stop_after_attempt(3))                                   # truth-absent: RETRY_UNBOUNDED
async def bounded(client: httpx.AsyncClient) -> None:
    await client.get("/health")


async def loop_forever(client: httpx.AsyncClient) -> None:
    while True:                                                                        # truth: RETRY_UNBOUNDED
        try:
            await client.get("/health")
            break
        except httpx.HTTPError:
            await asyncio.sleep(1)


async def loop_bounded(client: httpx.AsyncClient) -> None:
    for attempt in range(5):                                                           # truth-absent: RETRY_UNBOUNDED
        try:
            await client.get("/health")
            break
        except httpx.HTTPError:
            await asyncio.sleep(1)


async def publish_after_commit(session: AsyncSession, account: Account) -> None:
    session.add(account)
    await session.commit()
    await producer.send("accounts", {"id": account.id})                                # truth: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def publish_via_outbox(session: AsyncSession, account: Account) -> None:
    session.add(account)
    session.add(Outbox(payload="account.created"))
    await session.commit()                                                             # truth-absent: REL_DUAL_WRITE_WITHOUT_OUTBOX
