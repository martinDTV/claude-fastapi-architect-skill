"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ShipmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(min_length=1, max_length=40, description="the label printed on the parcel")
    weight_kg: Decimal | None = Field(default=None, ge=0)


class ShipmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reference: str | None = Field(
        default=None,
        min_length=1, max_length=40, description="the label printed on the parcel",
    )
    weight_kg: Decimal | None = Field(default=None, ge=0)


class ShipmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    reference: str
    weight_kg: Decimal | None
    created_at: datetime
