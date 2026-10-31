"""Composition of the module: repositories, ports and services as FastAPI dependencies. The session carries the tenant
context of the authenticated principal (TenantSessionDep), so every query runs under the row-level security policies.
"""

from typing import Annotated

from fastapi import Depends

from tenant_projects.core.db import TenantSessionDep
from tenant_projects.modules.tasks.repository import SqlTaskRepository, TaskRepository
from tenant_projects.modules.tasks.service import TaskService


def get_task_repository(session: TenantSessionDep) -> TaskRepository:
    return SqlTaskRepository(session)


def get_task_service(
    repository: Annotated[TaskRepository, Depends(get_task_repository)],
    session: TenantSessionDep,
) -> TaskService:
    return TaskService(repository, session)


TaskServiceDep = Annotated[TaskService, Depends(get_task_service)]
