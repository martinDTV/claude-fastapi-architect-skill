"""HTTP boundary of the notes module: schema validation, status codes, problem details for domain errors. Each route
carries the principal admitted for its operation; the service decides on the resource.
"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status

from team_notes.core.authz import CanCreate, CanDelete, CanGet, CanList, CanUpdate
from team_notes.dependencies.notes import NoteServiceDep, TagServiceDep
from team_notes.schemas.notes import NoteCreate, NoteRead, NoteUpdate, TagCreate, TagRead

router = APIRouter(tags=["notes"])


@router.post("/tenants/{workspace_id}/notes", status_code=status.HTTP_201_CREATED)
async def create_note(principal: CanCreate, payload: NoteCreate, service: NoteServiceDep) -> NoteRead:
    return NoteRead.model_validate(await service.create(principal, payload))


@router.get("/tenants/{workspace_id}/notes")
async def list_notes(
    principal: CanList,
    service: NoteServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[NoteRead]:
    return [NoteRead.model_validate(item) for item in await service.list(principal, limit, offset)]


@router.get("/tenants/{workspace_id}/notes/{note_id}")
async def get_note(principal: CanGet, note_id: UUID, service: NoteServiceDep) -> NoteRead:
    return NoteRead.model_validate(await service.get(principal, note_id))


@router.patch("/tenants/{workspace_id}/notes/{note_id}")
async def update_note(principal: CanUpdate, note_id: UUID, payload: NoteUpdate, service: NoteServiceDep) -> NoteRead:
    return NoteRead.model_validate(await service.update(principal, note_id, payload))


@router.delete("/tenants/{workspace_id}/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(principal: CanDelete, note_id: UUID, service: NoteServiceDep) -> None:
    await service.delete(principal, note_id)


@router.post("/tenants/{workspace_id}/tags", status_code=status.HTTP_201_CREATED)
async def create_tag(principal: CanCreate, payload: TagCreate, service: TagServiceDep) -> TagRead:
    return TagRead.model_validate(await service.create(principal, payload))


@router.get("/tenants/{workspace_id}/tags")
async def list_tags(
    principal: CanList,
    service: TagServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[TagRead]:
    return [TagRead.model_validate(item) for item in await service.list(principal, limit, offset)]


@router.get("/tenants/{workspace_id}/tags/{tag_id}")
async def get_tag(principal: CanGet, tag_id: UUID, service: TagServiceDep) -> TagRead:
    return TagRead.model_validate(await service.get(principal, tag_id))


@router.delete("/tenants/{workspace_id}/tags/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tag(principal: CanDelete, tag_id: UUID, service: TagServiceDep) -> None:
    await service.delete(principal, tag_id)
