"""P1: where locals come from decides how their methods are classified. No project edge is invented."""

import json

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from . import repo


async def fetch(client: httpx.AsyncClient) -> dict:
    response = await client.get("/x")
    data = response.json()                                                             # truth-unresolved: external
    return data.copy()                                                                 # truth-unresolved: external


def parse(raw: str) -> str:
    payload = json.loads(raw)
    return payload.get("a")                                                            # truth-unresolved: external


def split(line: str) -> str:
    name, value = line.split(":", 1)
    return name.strip() + value.strip()                                                # truth-unresolved: builtin


async def closure(session: AsyncSession) -> object:
    async def inner():
        return session.execute("select 1")                                             # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=verified

    return await inner()


def loop_targets(items: list[str]) -> list[str]:
    out = []
    for item in items:
        out.append(item.upper())                                                       # truth-unresolved: dynamic
    return out


def comprehension(items: list[str]) -> list[str]:
    return [item.lower() for item in items]                                            # truth-unresolved: dynamic


async def project_return(session: AsyncSession) -> str:
    account = await repo.get_account(session, 1)
    return account.describe()                                                          # truth-edge: svc.repo.Account.describe


async def external_return(session: AsyncSession) -> int:
    rows = await repo.list_rows(session)
    return rows.count({})                                                              # truth-unresolved: builtin


def label_from_project(session: AsyncSession) -> str:
    text = repo.label(repo.Account(1, 2))
    return text.upper()                                                                # truth-unresolved: builtin


async def unpack_project(session: AsyncSession) -> str:
    first, second = await repo.get_account(session, 1), await repo.get_account(session, 2)
    return first.describe() + second.describe()                                        # truth-edge: svc.repo.Account.describe
