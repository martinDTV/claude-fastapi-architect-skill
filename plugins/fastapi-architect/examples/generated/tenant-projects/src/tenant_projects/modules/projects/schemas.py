"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    budget: Decimal = Field(ge=0)
    starts_on: date


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    budget: Decimal | None = Field(default=None, ge=0)
    starts_on: date | None = Field(default=None)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    budget: Decimal
    starts_on: date
    created_at: datetime
