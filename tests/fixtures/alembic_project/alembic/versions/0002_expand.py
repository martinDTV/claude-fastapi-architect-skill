"""expand phase: additive, backwards compatible"""
import sqlalchemy as sa
from alembic import op
from alembic import op as operations

revision = "0002"
down_revision = "0001"


def upgrade() -> None:
    regions = op.create_table(
        "regions",
        sa.Column("code", sa.String(8), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
    )
    op.create_index("ix_regions_name", "regions", ["name"])               # new table: no lock concern
    op.bulk_insert(regions, [{"code": "EU", "name": "Europe"}])            # seed of the new table
    op.add_column("orders", sa.Column("region_code", sa.String(8), nullable=True))
    op.add_column("orders", sa.Column("flag", sa.Boolean(), nullable=False, server_default=sa.false()))
    with op.get_context().autocommit_block():
        op.create_index("ix_orders_region_code", "orders", ["region_code"], postgresql_concurrently=True)
    op.execute("ALTER TABLE orders ENABLE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY orders_tenant ON orders USING (tenant_id = current_setting('app.tenant_id')::uuid)")
    op.execute("DROP FUNCTION IF EXISTS legacy_fn()")
    op.drop_constraint("uq_orders_number", "orders", type_="unique")
    op.create_unique_constraint("uq_orders_number_region", "orders", ["number", "region_code"])
    operations.execute("INSERT INTO regions (code, name) VALUES ('US', 'United States')")


def downgrade() -> None:
    op.drop_constraint("uq_orders_number_region", "orders", type_="unique")
    op.drop_column("orders", "flag")
    op.drop_column("orders", "region_code")
    op.drop_table("regions")
