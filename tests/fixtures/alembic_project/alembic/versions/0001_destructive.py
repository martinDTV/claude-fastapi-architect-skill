"""destructive changes that need review"""
import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade() -> None:
    op.drop_table("legacy_events")
    op.drop_column("orders", "old_status")
    op.alter_column("orders", "amount", type_=sa.Numeric(12, 2))
    op.add_column("orders", sa.Column("region", sa.String(8), nullable=False))
    op.alter_column("orders", "currency", nullable=False)
    op.create_index("ix_orders_region", "orders", ["region"])
    op.execute("ALTER TABLE orders SET (fillfactor = 70)")
    op.execute("UPDATE orders SET region = 'EU' WHERE region IS NULL")
    op.drop_constraint("uq_orders_number", "orders", type_="unique")
    op.execute("DROP TABLE IF EXISTS scratch")


def downgrade() -> None:
    pass
