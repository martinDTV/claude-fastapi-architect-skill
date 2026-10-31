"""Entities of the module (plain dataclasses; the in-memory repository stores them as they are)."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4


@dataclass
class Product:
    sku: str
    name: str
    quantity: int
    unit_price: Decimal
    notes: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
