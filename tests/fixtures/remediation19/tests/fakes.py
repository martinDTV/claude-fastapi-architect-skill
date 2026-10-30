"""A fake AsyncSession faithful to the semantics the recipes rely on: begin() owns a transaction and
commits on exit, commit() inside the block closes it and any later operation raises, add() stages,
commit() publishes, execute() runs only when awaited."""

from __future__ import annotations

import sys
import types


class ClosedTransaction(RuntimeError):
    pass


class FakeAsyncSession:
    def __init__(self, fail_on_add_number: int = 0) -> None:
        self.pending: list = []
        self.committed: list = []
        self.executed: list = []
        self.commits = 0
        self.adds = 0
        self._fail_on = fail_on_add_number
        self._in_block = False
        self._closed = False

    def _check(self) -> None:
        if self._in_block and self._closed:
            raise ClosedTransaction("Can't operate on closed transaction inside context manager")

    def add(self, obj) -> None:
        self._check()
        self.adds += 1
        if self._fail_on and self.adds == self._fail_on:
            raise ValueError("boom")
        self.pending.append(obj)

    async def execute(self, statement):
        self._check()
        self.executed.append(statement)
        return statement

    async def commit(self) -> None:
        self._check()
        self.committed.extend(self.pending)
        self.pending.clear()
        self.commits += 1
        if self._in_block:
            self._closed = True

    async def rollback(self) -> None:
        self.pending.clear()

    def begin(self):
        session = self

        class _Begin:
            async def __aenter__(self):
                session._in_block, session._closed = True, False
                return session

            async def __aexit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None and not session._closed:
                        await session.commit()
                finally:
                    session._in_block, session._closed = False, False
                return False

        return _Begin()


def stub_sqlalchemy_if_missing() -> None:
    """The service imports AsyncSession for its annotations only; install an import-only stub when the
    real package is absent so the fixture runs on a bare interpreter."""
    try:
        import sqlalchemy.ext.asyncio  # noqa: F401
    except Exception:
        root = types.ModuleType("sqlalchemy")
        ext = types.ModuleType("sqlalchemy.ext")
        aio = types.ModuleType("sqlalchemy.ext.asyncio")
        aio.AsyncSession = FakeAsyncSession  # type: ignore[attr-defined]
        root.ext = ext  # type: ignore[attr-defined]
        ext.asyncio = aio  # type: ignore[attr-defined]
        sys.modules.setdefault("sqlalchemy", root)
        sys.modules.setdefault("sqlalchemy.ext", ext)
        sys.modules.setdefault("sqlalchemy.ext.asyncio", aio)
