"""SQLAlchemy 2 mapped classes of the module (tables are created by Alembic, never by create_all)."""

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Numeric, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from parcels.core.db import Base


class Shipment(Base):
    __tablename__ = "shipments"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    reference: Mapped[str] = mapped_column(String(40))
    weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
