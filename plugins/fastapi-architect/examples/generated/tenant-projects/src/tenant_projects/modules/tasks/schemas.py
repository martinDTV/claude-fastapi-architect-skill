"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID
    title: str = Field(min_length=1, max_length=200)
    done: bool = Field(default=False)


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: UUID | None = Field(default=None)
    title: str | None = Field(default=None, min_length=1, max_length=200)
    done: bool | None = Field(default=None)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    owner_id: str
    project_id: UUID
    title: str
    done: bool
    created_at: datetime
