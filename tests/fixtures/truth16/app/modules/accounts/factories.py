import time
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from .repository import AccountRepository


def make_repo(session: AsyncSession) -> AccountRepository:
    return AccountRepository(session)


async def use_factory(session: AsyncSession, account_id: UUID):
    repo = make_repo(session)
    return await repo.get(account_id)                                                  # truth-edge: app.modules.accounts.repository.AccountRepository.get


class Tools:
    @staticmethod
    def helper() -> None:
        time.sleep(1)

    @classmethod
    def build(cls) -> "Tools":
        return cls()


async def uses_static() -> None:
    Tools.helper()                                                                     # truth: ASYNC_BLOCKING_CALL_INDIRECT


async def uses_classmethod() -> Tools:
    return Tools.build()                                                               # truth-edge: app.modules.accounts.factories.Tools.build


def count(items: list) -> int:
    return len(items)                                                                  # truth-unresolved: builtin
