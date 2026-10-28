"""contract phase after the backfill finished"""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute("UPDATE orders SET region_code = 'EU' WHERE region_code IS NULL")
    op.alter_column("orders", "region_code", nullable=False)            # backfill visible above
    op.execute(f"CREATE INDEX ix_orders_{'tenant'} ON orders (tenant_id)")  # dynamic SQL


def downgrade() -> None:
    raise RuntimeError("irreversible: documented in ADR 0007")
