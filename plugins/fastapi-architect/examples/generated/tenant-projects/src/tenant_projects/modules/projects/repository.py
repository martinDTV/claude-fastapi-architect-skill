"""Repositories: a Protocol per entity and the implementation chosen by the design."""

from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tenant_projects.modules.projects.models import Project


class ProjectRepository(Protocol):
    async def add(self, project: Project) -> Project: ...

    async def get(self, tenant_id: UUID, project_id: UUID) -> Project | None: ...

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Project]: ...

    async def save(self, project: Project) -> Project: ...

    async def delete(self, tenant_id: UUID, project_id: UUID) -> bool: ...


class SqlProjectRepository:
    """Repository on the request-scoped AsyncSession; the service owns the flush and the commit (unit of work)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, project: Project) -> Project:
        self._session.add(project)
        return project

    async def get(self, tenant_id: UUID, project_id: UUID) -> Project | None:
        statement = select(Project).where(Project.id == project_id).where(Project.tenant_id == tenant_id)
        return await self._session.scalar(statement)

    async def list(self, tenant_id: UUID, limit: int, offset: int) -> list[Project]:
        statement = select(Project).where(Project.tenant_id == tenant_id).order_by(Project.created_at, Project.id)
        rows = await self._session.scalars(statement.limit(limit).offset(offset))
        return list(rows)

    async def save(self, project: Project) -> Project:
        return project  # tracked by the session; written at commit

    async def delete(self, tenant_id: UUID, project_id: UUID) -> bool:
        item = await self.get(tenant_id, project_id)
        if item is None:
            return False
        await self._session.delete(item)
        return True
