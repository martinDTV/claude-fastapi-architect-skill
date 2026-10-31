"""Use cases of the module: validation beyond the schema, domain errors, one commit per use case. Ownership is enforced
here and in the repository query (read_scope / ensure_owner), never in the router alone.
"""

from uuid import UUID

from team_notes.core.authz import ensure_owner, read_scope
from team_notes.core.errors import NotFoundError
from team_notes.core.security import Principal
from team_notes.models.notes import Note, Tag
from team_notes.repositories.notes import NoteRepository, TagRepository
from team_notes.schemas.notes import NoteCreate, NoteUpdate, TagCreate


class NoteService:
    def __init__(self, repository: NoteRepository) -> None:
        self._repository = repository

    async def create(self, principal: Principal, data: NoteCreate) -> Note:
        note = Note(
            workspace_id=principal.tenant_id,
            owner_id=principal.subject,
            title=data.title,
            body=data.body,
            pinned=data.pinned,
        )
        await self._repository.add(note)
        return note

    async def get(self, principal: Principal, note_id: UUID) -> Note:
        note = await self._repository.get(principal.tenant_id, note_id, owner=read_scope(principal))
        if note is None:
            raise NotFoundError(f"Note {note_id} not found")
        return note

    async def list(self, principal: Principal, limit: int = 50, offset: int = 0) -> list[Note]:
        return await self._repository.list(principal.tenant_id, limit, offset, owner=read_scope(principal))

    async def update(self, principal: Principal, note_id: UUID, data: NoteUpdate) -> Note:
        note = await self.get(principal, note_id)
        ensure_owner(principal, note.owner_id, "update", "Note")
        for name, value in data.model_dump(exclude_unset=True).items():
            setattr(note, name, value)
        await self._repository.save(note)
        return note

    async def delete(self, principal: Principal, note_id: UUID) -> None:
        note = await self.get(principal, note_id)
        ensure_owner(principal, note.owner_id, "delete", "Note")
        await self._repository.delete(principal.tenant_id, note.id)


class TagService:
    def __init__(self, repository: TagRepository) -> None:
        self._repository = repository

    async def create(self, principal: Principal, data: TagCreate) -> Tag:
        tag = Tag(workspace_id=principal.tenant_id, label=data.label)
        await self._repository.add(tag)
        return tag

    async def get(self, principal: Principal, tag_id: UUID) -> Tag:
        tag = await self._repository.get(principal.tenant_id, tag_id)
        if tag is None:
            raise NotFoundError(f"Tag {tag_id} not found")
        return tag

    async def list(self, principal: Principal, limit: int = 50, offset: int = 0) -> list[Tag]:
        return await self._repository.list(principal.tenant_id, limit, offset)

    async def delete(self, principal: Principal, tag_id: UUID) -> None:
        removed = await self._repository.delete(principal.tenant_id, tag_id)
        if not removed:
            raise NotFoundError(f"Tag {tag_id} not found")
