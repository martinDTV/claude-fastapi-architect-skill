"""Entities of the module (plain dataclasses; the in-memory repository stores them as they are)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4


@dataclass
class Note:
    workspace_id: UUID
    owner_id: str  # subject of the principal that created the row
    title: str
    body: str
    pinned: bool = False
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Tag:
    workspace_id: UUID
    label: str
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
