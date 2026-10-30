"""P3: attribute chains, mixins, inherited hooks, external bases. Ambiguity never becomes a finding."""

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session


class Item:
    pass


class Repo:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, item_id: int):
        return await self._session.get(Item, item_id)


class UnitOfWork:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo


class Service:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    async def load(self, item_id: int):
        return await self._uow.repo.get(item_id)                                       # truth-edge: svc.attrs.Repo.get


class SessionMixin:
    async def count(self):
        result = self._session.execute("select count(*) from items")                  # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=inferred
        return result


class ItemRepo(SessionMixin):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class ConflictMixin:
    async def total(self):
        result = self._session.execute("select sum(x) from items")                    # truth-absent: SA_ASYNC_SESSION_NOT_AWAITED
        return result


class AsyncUser(ConflictMixin):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class SyncUser(ConflictMixin):
    def __init__(self, session: Session) -> None:
        self._session = session


class HookBase:
    async def run(self):
        return await self.step()                                                       # truth-unresolved: ambiguous


class HookA(HookBase):
    async def step(self):
        return "a"


class HookB(HookBase):
    async def step(self):
        return "b"


class SingleHookBase:
    async def run(self):
        return await self.step()                                                       # truth-edge: svc.attrs.OnlyImpl.step


class OnlyImpl(SingleHookBase):
    async def step(self):
        return "only"


class Payload(BaseModel):
    name: str

    def as_json(self) -> str:
        return self.model_dump_json()                                                  # truth-unresolved: external
