from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class Account:
    def __init__(self, id: int, balance: int) -> None:
        self.id = id
        self.balance = balance

    def describe(self) -> str:
        return f"{self.id}:{self.balance}"


async def get_account(session: AsyncSession, account_id: int) -> Account:
    row = await session.execute(text("select id, balance from accounts where id = :id"), {"id": account_id})
    return Account(*row.one())


async def list_rows(session: AsyncSession) -> list[dict]:
    rows = await session.execute(text("select id from accounts"))
    return [dict(r) for r in rows.mappings()]


def label(account: Account) -> str:
    return account.describe()
