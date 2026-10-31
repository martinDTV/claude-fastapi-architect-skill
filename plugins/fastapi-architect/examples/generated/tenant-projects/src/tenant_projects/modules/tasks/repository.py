"""Repositories: a Protocol per entity and the implementation chosen by the design. Owned entities take an optional
owner: when given, reads are restricted to that owner's rows in the query itself.
"""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenant_projects.modules.tasks.models import Task


class TaskRepository(Protocol):
    async def add(self, task: Task) -> Task: ...

    async def get(self, tenant_id: UUID, task_id: UUID, owner: str | None = None) -> Task | None: ...

    async def list(self, tenant_id: UUID, limit: int, offset: int, owner: str | None = None) -> list[Task]: ...

    async def save(self, task: Task) -> Task: ...

    async def delete(self, tenant_id: UUID, task_id: UUID) -> bool: ...


class SqlTaskRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, task: Task) -> Task:
        self._session.add(task)
        return task

    async def get(self, tenant_id: UUID, task_id: UUID, owner: str | None = None) -> Task | None:
        statement = select(Task).where(Task.id == task_id).where(Task.tenant_id == tenant_id)
        if owner is not None:
            statement = statement.where(Task.owner_id == owner)
        return await self._session.scalar(statement)

    async def list(self, tenant_id: UUID, limit: int, offset: int, owner: str | None = None) -> list[Task]:
        statement = select(Task).where(Task.tenant_id == tenant_id).order_by(Task.created_at, Task.id)
        if owner is not None:
            statement = statement.where(Task.owner_id == owner)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)

    async def save(self, task: Task) -> Task:
        return task  # tracked by the session; written at commit

    async def delete(self, tenant_id: UUID, task_id: UUID) -> bool:
        item = await self.get(tenant_id, task_id)
        if item is None:
            return False
        await self._session.delete(item)
        return True
