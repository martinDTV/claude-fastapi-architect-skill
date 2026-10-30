from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Contact


class Finder:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find(self, email: str) -> Contact:
        return Contact(email)


class Context:
    def __init__(self, finder: Finder) -> None:
        self.finder = finder


class Handler:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    async def handle(self, email: str) -> Contact:
        return await self.ctx.finder.find(email)                                       # truth-edge: crm.layers.Finder.find


class AuditedMixin:
    async def audit(self) -> None:
        self._session.execute("insert into audit values (1)")                          # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=inferred


class ContactRepo(AuditedMixin):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class Event(BaseModel):
    kind: str

    def payload(self) -> dict:
        return self.model_dump()                                                       # truth-unresolved: external


class Template:
    async def execute(self):
        return await self.perform()                                                    # truth-edge: crm.layers.Concrete.perform


class Concrete(Template):
    async def perform(self):
        return 1
