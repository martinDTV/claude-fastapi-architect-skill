"""Versioned API router: every business router is mounted under /api/v1 behind the API key."""

from fastapi import APIRouter, Depends

from tenant_projects.core.security import require_api_key
from tenant_projects.modules.projects.router import router as projects_router
from tenant_projects.modules.tasks.router import router as tasks_router

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_api_key)])
api_router.include_router(projects_router)
api_router.include_router(tasks_router)
