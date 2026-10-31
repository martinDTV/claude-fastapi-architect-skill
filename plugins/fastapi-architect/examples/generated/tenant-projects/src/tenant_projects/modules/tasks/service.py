"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case. Ownership is enforced
here and in the repository query (read_scope / ensure_owner), never in the router alone.
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenant_projects.core.authz import ensure_owner, read_scope
from tenant_projects.core.errors import ConflictError, NotFoundError
from tenant_projects.core.security import Principal
from tenant_projects.modules.tasks.models import Task
from tenant_projects.modules.tasks.repository import TaskRepository
from tenant_projects.modules.tasks.schemas import TaskCreate, TaskUpdate


class TaskService:
    def __init__(self, repository: TaskRepository, session: AsyncSession) -> None:
        self._repository = repository
        self._session = session

    async def _commit(self) -> None:
        """End of the unit of work (flush + commit): a constraint violation is rolled back and reported as 409."""
        try:
            await self._session.commit()
        except IntegrityError as exc:
            await self._session.rollback()
            detail = f"Task violates a database constraint: {exc.orig}"
            raise ConflictError(detail) from exc

    async def create(self, principal: Principal, data: TaskCreate) -> Task:
        task = Task(
            tenant_id=principal.tenant_id,
            owner_id=principal.subject,
            project_id=data.project_id,
            title=data.title,
            done=data.done,
        )
        await self._repository.add(task)
        await self._commit()
        return task

    async def get(self, principal: Principal, task_id: UUID) -> Task:
        task = await self._repository.get(principal.tenant_id, task_id, owner=read_scope(principal))
        if task is None:
            raise NotFoundError(f"Task {task_id} not found")
        return task

    async def list(self, principal: Principal, limit: int = 50, offset: int = 0) -> list[Task]:
        return await self._repository.list(principal.tenant_id, limit, offset, owner=read_scope(principal))

    async def update(self, principal: Principal, task_id: UUID, data: TaskUpdate) -> Task:
        task = await self.get(principal, task_id)
        ensure_owner(principal, task.owner_id, "update", "Task")
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(task, name, value)
        await self._repository.save(task)
        await self._commit()
        return task

    async def delete(self, principal: Principal, task_id: UUID) -> None:
        task = await self.get(principal, task_id)
        ensure_owner(principal, task.owner_id, "delete", "Task")
        await self._repository.delete(principal.tenant_id, task.id)
        await self._commit()
