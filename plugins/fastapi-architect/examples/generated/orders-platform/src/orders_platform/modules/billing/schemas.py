"""Request and response schemas (pydantic v2); unknown properties are rejected."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class InvoiceCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    order_id: UUID
    amount: Decimal = Field(ge=0)
    issued_on: date


class InvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    order_id: UUID
    amount: Decimal
    issued_on: date
    created_at: datetime
