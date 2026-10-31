"""Initial schema of parcels: generated from the architecture contract (digest 498a527757ec).

Revision ID: 8154db01f010
Revises: none (initial)
Create Date: derived from the contract, not from the clock (reproducible)
"""

import sqlalchemy as sa
from alembic import op

revision = "8154db01f010"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "shipments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(length=40), nullable=False),
        sa.Column("weight_kg", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_shipments"),
    )


def downgrade() -> None:
    op.drop_table("shipments")
