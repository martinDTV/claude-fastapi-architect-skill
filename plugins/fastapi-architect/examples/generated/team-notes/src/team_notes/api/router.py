"""Versioned API router: every business router is mounted under /api/v1 behind the bearer token."""

from fastapi import APIRouter, Depends

from team_notes.api.notes import router as notes_router
from team_notes.core.security import require_bearer_token

api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_bearer_token)])
api_router.include_router(notes_router)
