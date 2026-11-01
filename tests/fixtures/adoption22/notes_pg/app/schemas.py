"""Pydantic schemas (hand-written)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    body: str = ""


class NoteOut(NoteIn):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    created_at: datetime
