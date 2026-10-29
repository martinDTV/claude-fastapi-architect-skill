import asyncio
from uuid import UUID

import httpx
from sqlalchemy import text

from app.core import AsyncSession
from app.modules.notify.email import EmailNotifier

from .ports import AccountPort, NotifierPort
from .repository import AccountRepository


class AccountService:
    def __init__(self, repo: AccountPort, notifier: NotifierPort, session: AsyncSession, client: httpx.AsyncClient) -> None:
        self._repo = repo
        self._notifier = notifier
        self._session = session
        self._client = client
        self._primary = AccountRepository(session)

    async def _debit(self, account_id: UUID) -> None:
        await self._session.execute(text("update accounts set balance = balance - 1 where id = :id"), {"id": account_id})

    async def _credit(self, account_id: UUID) -> None:
        await self._session.execute(text("update accounts set balance = balance + 1 where id = :id"), {"id": account_id})

    async def transfer(self, src: UUID, dst: UUID) -> None:
        await asyncio.gather(self._debit(src), self._credit(dst))                       # truth: SA_SESSION_SHARED_CONCURRENTLY

    async def transfer_sequential(self, src: UUID, dst: UUID) -> None:
        await self._debit(src)                                                           # truth-absent: SA_SESSION_SHARED_CONCURRENTLY
        await self._credit(dst)

    async def fetch_both(self) -> tuple:
        return await asyncio.gather(self._client.get("/a"), self._client.get("/b"))     # truth-absent: SA_SESSION_SHARED_CONCURRENTLY

    async def notify_in_tx(self, account_id: UUID) -> None:
        async with self._session.begin():
            await self._client.post("/notify", json={"id": str(account_id)})            # truth: TX_EXTERNAL_CALL_IN_TRANSACTION

    async def through_port(self, account_id: UUID):
        return await self._repo.get(account_id)                                          # truth-unresolved: ambiguous

    async def send_welcome(self, to: str) -> None:
        self._notifier.send(to, "welcome")                                               # truth: ASYNC_BLOCKING_CALL_INDIRECT

    async def direct_repo(self, repo: AccountRepository, account_id: UUID):
        return await repo.get(account_id)                                                # truth-edge: app.modules.accounts.repository.AccountRepository.get

    async def through_attribute(self, account_id: UUID):
        return await self._primary.get(account_id)                                       # truth-edge: app.modules.accounts.repository.AccountRepository.get


async def summary(session: AsyncSession, tenant_id: UUID) -> dict:
    rows = await session.execute(text("select count(*) from accounts where tenant_id = :t"), {"t": tenant_id})
    return {"count": rows.scalar_one()}


async def summary_for(session: AsyncSession, ctx, tenant_id: UUID) -> dict:
    if ctx.user.id is None:
        raise PermissionError
    return await summary(session, tenant_id)                                             # truth-edge: app.modules.accounts.service.summary


def make_notifier() -> NotifierPort:
    return EmailNotifier()
