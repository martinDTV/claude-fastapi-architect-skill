"""P3: attribute chains, mixins, hooks and external bases, conservatively."""

import time
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession


class Finder:
    def find(self, code: str) -> str:
        time.sleep(0.1)
        return code


class Context:
    def __init__(self, finder: Finder) -> None:
        self.finder = finder


class Handler:
    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx

    async def handle(self, code: str) -> str:
        return self.ctx.finder.find(code)                                              # truth-edge: wms.layers.Finder.find


class AuditedMixin:
    async def audit(self) -> None:
        self._session.execute("select 1")                                              # truth: SA_ASYNC_SESSION_NOT_AWAITED evidence=inferred


class StockRepo(AuditedMixin):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class LooseMixin:
    async def audit(self) -> None:
        self._session.execute("select 1")                                              # truth-absent: SA_ASYNC_SESSION_NOT_AWAITED


class TypedUser(LooseMixin):
    def __init__(self, session: AsyncSession) -> None:
        self._session = session


class UntypedUser(LooseMixin):
    def __init__(self, session) -> None:
        self._session = session


class Template:
    async def execute(self) -> str:
        return self.perform()                                                          # truth-edge: wms.layers.Concrete.perform


class Concrete(Template):
    def perform(self) -> str:
        return "done"


class Base:
    async def run(self) -> str:
        return self.step()                                                             # truth-unresolved: ambiguous


class ImplA(Base):
    def step(self) -> str:
        return "a"


class ImplB(Base):
    def step(self) -> str:
        return "b"


class State(TypedDict, total=False):
    code: str


def read_state(state: State) -> str:
    return state.get("code", "")                                                       # truth-unresolved: external


class WmsError(Exception):
    def annotate(self) -> None:
        self.add_note("wms")                                                           # truth-unresolved: external
