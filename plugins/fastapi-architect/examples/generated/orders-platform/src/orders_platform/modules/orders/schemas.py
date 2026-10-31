"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: UUID
    total_amount: Decimal = Field(ge=0)
    currency: str = Field(min_length=1, max_length=3)
    placed_at: datetime


class OrderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    customer_id: UUID
    total_amount: Decimal
    currency: str
    placed_at: datetime
    created_at: datetime
