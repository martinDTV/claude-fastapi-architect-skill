from uuid import UUID

from sqlalchemy import select

from app.core import AsyncSession  # external type re-exported through the project facade

from .models import Account


class AccountRepository:
    """Session held as an instance attribute: kinds must flow from __init__ to the methods."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, account_id: UUID):
        result = self._session.execute(select(Account).where(Account.id == account_id))   # truth: SA_ASYNC_SESSION_NOT_AWAITED
        return result

    async def save(self, account: Account) -> None:
        self._session.add(account)                                                         # truth-absent: SA_ASYNC_SESSION_NOT_AWAITED
        await self._session.commit()

    async def bulk(self, accounts: list[Account]) -> None:
        for account in accounts:
            self._session.add(account)
            await self._session.commit()                                                   # truth: TX_COMMIT_IN_LOOP


class CachedAccountRepository(AccountRepository):
    pass


class MemoryAccountRepository:
    """Structural implementation of AccountPort without inheritance."""

    def __init__(self) -> None:
        self._rows: dict = {}

    async def get(self, account_id: UUID):
        return self._rows.get(account_id)                                                  # truth-unresolved: builtin

    async def save(self, account) -> None:
        self._rows[account.id] = account
