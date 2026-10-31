"""Composition of the module: repositories, ports and services as FastAPI dependencies. The session carries the tenant
context of the authenticated principal (TenantSessionDep), so every query runs under the row-level security policies.
"""

from typing import Annotated

from fastapi import Depends

from tenant_projects.core.db import TenantSessionDep
from tenant_projects.modules.projects.repository import ProjectRepository, SqlProjectRepository
from tenant_projects.modules.projects.service import ProjectService


def get_project_repository(session: TenantSessionDep) -> ProjectRepository:
    return SqlProjectRepository(session)


def get_project_service(
    repository: Annotated[ProjectRepository, Depends(get_project_repository)],
    session: TenantSessionDep,
) -> ProjectService:
    return ProjectService(repository, session)


ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
