"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ProductCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str = Field(min_length=1, max_length=32, description="Stock keeping unit, unique per warehouse")
    name: str = Field(min_length=1, max_length=120)
    quantity: int = Field(ge=0)
    unit_price: Decimal = Field(ge=0)
    notes: str | None = Field(default=None, min_length=1, max_length=500)


class ProductUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sku: str | None = Field(
        default=None,
        min_length=1, max_length=32, description="Stock keeping unit, unique per warehouse",
    )
    name: str | None = Field(default=None, min_length=1, max_length=120)
    quantity: int | None = Field(default=None, ge=0)
    unit_price: Decimal | None = Field(default=None, ge=0)
    notes: str | None = Field(default=None, min_length=1, max_length=500)


class ProductRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    notes: str | None
    created_at: datetime
