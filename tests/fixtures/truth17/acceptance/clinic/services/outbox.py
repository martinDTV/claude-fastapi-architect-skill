import backoff
import httpx
import tenacity
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

sqs = None  # boto3 client bound at startup


async def publish_after_commit(db: AsyncSession, payload: str) -> None:
    await db.execute(text("insert into visits (payload) values (:p)"), {"p": payload})
    await db.commit()
    sqs.send_message(QueueUrl="q", MessageBody=payload)                                # truth: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def publish_through_outbox(db: AsyncSession, payload: str) -> None:
    await db.execute(text("insert into visits (payload) values (:p)"), {"p": payload})
    await db.execute(text("insert into outbox_events (payload) values (:p)"), {"p": payload})
    await db.commit()                                                                  # truth-absent: REL_DUAL_WRITE_WITHOUT_OUTBOX


async def handle_delivery(db: AsyncSession, delivery_id: str, payload: str) -> None:
    seen = await db.execute(text("insert into processed_deliveries (id) values (:i) on conflict (id) do nothing returning id"), {"i": delivery_id})
    if seen.scalar_one_or_none() is None:
        return                                                                         # redelivery: already handled
    await db.execute(text("update visits set confirmed = true where payload = :p"), {"p": payload})
    await db.commit()


@tenacity.retry(stop=tenacity.stop_after_delay(30))                                   # truth-absent: RETRY_UNBOUNDED
async def notify(http: httpx.AsyncClient, visit_id: str) -> None:
    await http.post("/notify", json={"visit": visit_id})                               # truth: RETRY_NON_IDEMPOTENT_WRITE


@backoff.on_exception(backoff.expo, httpx.HTTPError)                                  # truth: RETRY_UNBOUNDED
async def heartbeat(http: httpx.AsyncClient) -> None:
    await http.get("/heartbeat")


async def mark_failed_after_retries(db: AsyncSession, http: httpx.AsyncClient, visit_id: str) -> None:
    for attempt in range(3):                                                           # truth-absent: RETRY_NON_IDEMPOTENT_WRITE
        try:
            return await http.get(f"/visits/{visit_id}")
        except httpx.HTTPError:
            if attempt == 2:
                await db.execute(text("update visits set failed = true where id = :i"), {"i": visit_id})
                await db.commit()
                raise
