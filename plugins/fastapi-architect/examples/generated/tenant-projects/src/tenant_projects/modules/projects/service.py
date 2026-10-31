"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case."""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenant_projects.core.errors import ConflictError, NotFoundError
from tenant_projects.core.security import Principal
from tenant_projects.modules.projects.models import Project
from tenant_projects.modules.projects.repository import ProjectRepository
from tenant_projects.modules.projects.schemas import ProjectCreate, ProjectUpdate


class ProjectService:
    def __init__(self, repository: ProjectRepository, session: AsyncSession) -> None:
        self._repository = repository
        self._session = session

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Project violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, principal: Principal, data: ProjectCreate) -> Project:
        project = Project(tenant_id=principal.tenant_id, name=data.name, budget=data.budget, starts_on=data.starts_on)
        await self._repository.add(project)
        await self._commit()
        return project

    async def get(self, principal: Principal, project_id: UUID) -> Project:
        project = await self._repository.get(principal.tenant_id, project_id)
        if project is None:
            raise NotFoundError(f"Project {project_id} not found")
        return project

    async def list(self, principal: Principal, limit: int = 50, offset: int = 0) -> list[Project]:
        return await self._repository.list(principal.tenant_id, limit, offset)

    async def update(self, principal: Principal, project_id: UUID, data: ProjectUpdate) -> Project:
        project = await self.get(principal, project_id)
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(project, name, value)
        await self._repository.save(project)
        await self._commit()
        return project

    async def delete(self, principal: Principal, project_id: UUID) -> None:
        removed = await self._repository.delete(principal.tenant_id, project_id)
        if not removed:
            raise NotFoundError(f"Project {project_id} not found")
        await self._commit()
