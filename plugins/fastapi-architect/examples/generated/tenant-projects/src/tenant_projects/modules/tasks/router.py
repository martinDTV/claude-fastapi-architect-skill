"""HTTP boundary of the tasks module: schema validation, status codes, problem details for domain errors. Each route
carries the principal admitted for its operation; the service decides on the resource.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from tenant_projects.core.authz import CanCreate, CanDelete, CanGet, CanList, CanUpdate
from tenant_projects.modules.tasks.dependencies import TaskServiceDep
from tenant_projects.modules.tasks.schemas import TaskCreate, TaskRead, TaskUpdate

router = APIRouter(tags=["tasks"])


@router.post("/tenants/{tenant_id}/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(principal: CanCreate, payload: TaskCreate, service: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await service.create(principal, payload))


@router.get("/tenants/{tenant_id}/tasks")
async def list_tasks(
    principal: CanList,
    service: TaskServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TaskRead]:
    return [TaskRead.model_validate(item) for item in await service.list(principal, limit, offset)]


@router.get("/tenants/{tenant_id}/tasks/{task_id}")
async def get_task(principal: CanGet, task_id: UUID, service: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await service.get(principal, task_id))


@router.patch("/tenants/{tenant_id}/tasks/{task_id}")
async def update_task(principal: CanUpdate, task_id: UUID, payload: TaskUpdate, service: TaskServiceDep) -> TaskRead:
    return TaskRead.model_validate(await service.update(principal, task_id, payload))


@router.delete("/tenants/{tenant_id}/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(principal: CanDelete, task_id: UUID, service: TaskServiceDep) -> None:
    await service.delete(principal, task_id)
