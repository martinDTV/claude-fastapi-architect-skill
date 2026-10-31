"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=1, max_length=254)
    display_name: str = Field(min_length=1, max_length=120)
    active: bool | None = Field(default=None)


class CustomerUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str | None = Field(default=None, min_length=1, max_length=254)
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    active: bool | None = Field(default=None)


class CustomerRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    active: bool | None
    created_at: datetime
