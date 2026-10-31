"""Repositories: a Protocol per entity and the implementation chosen by the design. Owned entities take an optional
owner: when given, reads are restricted to that owner's rows in the query itself.
"""

from typing import Protocol
from uuid import UUID

from team_notes.models.notes import Note, Tag


class NoteRepository(Protocol):
    async def add(self, note: Note) -> Note: ...

    async def get(self, workspace_id: UUID, note_id: UUID, owner: str | None = None) -> Note | None: ...

    async def list(self, workspace_id: UUID, limit: int, offset: int, owner: str | None = None) -> list[Note]: ...

    async def save(self, note: Note) -> Note: ...

    async def delete(self, workspace_id: UUID, note_id: UUID) -> bool: ...


class InMemoryNoteRepository:
    """Process-local store; the lifespan creates one per application, tests get a fresh one per app."""

    def __init__(self) -> None:
        self._items: dict[UUID, Note] = {}

    async def add(self, note: Note) -> Note:
        self._items[note.id] = note
        return note

    async def get(self, workspace_id: UUID, note_id: UUID, owner: str | None = None) -> Note | None:
        item = self._items.get(note_id)
        if item is None or item.workspace_id != workspace_id or (owner is not None and item.owner_id != owner):
            return None
        return item

    async def list(self, workspace_id: UUID, limit: int, offset: int, owner: str | None = None) -> list[Note]:
        scoped = [
            item
            for item in self._items.values()
            if item.workspace_id == workspace_id and (owner is None or item.owner_id == owner)
        ]
        items = sorted(scoped, key=lambda item: (item.created_at, str(item.id)))
        return items[offset : offset + limit]

    async def save(self, note: Note) -> Note:
        self._items[note.id] = note
        return note

    async def delete(self, workspace_id: UUID, note_id: UUID) -> bool:
        item = await self.get(workspace_id, note_id)
        if item is None:
            return False
        del self._items[note_id]
        return True


class TagRepository(Protocol):
    async def add(self, tag: Tag) -> Tag: ...

    async def get(self, workspace_id: UUID, tag_id: UUID) -> Tag | None: ...

    async def list(self, workspace_id: UUID, limit: int, offset: int) -> list[Tag]: ...

    async def delete(self, workspace_id: UUID, tag_id: UUID) -> bool: ...


class InMemoryTagRepository:
    """Process-local store; the lifespan creates one per application, tests get a fresh one per app."""

    def __init__(self) -> None:
        self._items: dict[UUID, Tag] = {}

    async def add(self, tag: Tag) -> Tag:
        self._items[tag.id] = tag
        return tag

    async def get(self, workspace_id: UUID, tag_id: UUID) -> Tag | None:
        item = self._items.get(tag_id)
        if item is None or item.workspace_id != workspace_id:
            return None
        return item

    async def list(self, workspace_id: UUID, limit: int, offset: int) -> list[Tag]:
        scoped = [item for item in self._items.values() if item.workspace_id == workspace_id]
        items = sorted(scoped, key=lambda item: (item.created_at, str(item.id)))
        return items[offset : offset + limit]

    async def delete(self, workspace_id: UUID, tag_id: UUID) -> bool:
        item = await self.get(workspace_id, tag_id)
        if item is None:
            return False
        del self._items[tag_id]
        return True
