"""SQLAlchemy 2 mapped classes of the module (tables are created by Alembic, never by create_all)."""

from datetime import date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import Date, DateTime, ForeignKeyConstraint, Numeric, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from orders_platform.core.db import Base


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_invoices_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "order_id"],
            ["orders.tenant_id", "orders.id"],
            name="fk_invoices_tenant_id_order_id_orders",
            ondelete="RESTRICT",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(18, 2))
    issued_on: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
