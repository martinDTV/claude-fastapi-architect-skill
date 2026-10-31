"""HTTP boundary of the projects module: schema validation, status codes, problem details for domain errors. Each route
carries the principal admitted for its operation; the service decides on the resource.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from tenant_projects.core.authz import CanCreate, CanDelete, CanGet, CanList, CanUpdate
from tenant_projects.modules.projects.dependencies import ProjectServiceDep
from tenant_projects.modules.projects.schemas import ProjectCreate, ProjectRead, ProjectUpdate

router = APIRouter(tags=["projects"])


@router.post("/tenants/{tenant_id}/projects", status_code=status.HTTP_201_CREATED)
async def create_project(principal: CanCreate, payload: ProjectCreate, service: ProjectServiceDep) -> ProjectRead:
    return ProjectRead.model_validate(await service.create(principal, payload))


@router.get("/tenants/{tenant_id}/projects")
async def list_projects(
    principal: CanList,
    service: ProjectServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ProjectRead]:
    return [ProjectRead.model_validate(item) for item in await service.list(principal, limit, offset)]


@router.get("/tenants/{tenant_id}/projects/{project_id}")
async def get_project(principal: CanGet, project_id: UUID, service: ProjectServiceDep) -> ProjectRead:
    return ProjectRead.model_validate(await service.get(principal, project_id))


@router.patch("/tenants/{tenant_id}/projects/{project_id}")
async def update_project(
    principal: CanUpdate,
    project_id: UUID,
    payload: ProjectUpdate,
    service: ProjectServiceDep,
) -> ProjectRead:
    return ProjectRead.model_validate(await service.update(principal, project_id, payload))


@router.delete("/tenants/{tenant_id}/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(principal: CanDelete, project_id: UUID, service: ProjectServiceDep) -> None:
    await service.delete(principal, project_id)
