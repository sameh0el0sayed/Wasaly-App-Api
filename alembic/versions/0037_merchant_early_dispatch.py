"""Merchant API: ready_at / is_ready and cancellation fee columns (additive)

Revision ID: 0037
Revises: 0036
"""
import sqlalchemy as sa

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("merchant_deliveries", sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("merchant_deliveries", sa.Column("is_ready", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("merchant_deliveries", sa.Column("cancel_fee", sa.String(20), nullable=True))
    op.add_column("merchant_deliveries", sa.Column("cancel_fee_payer", sa.String(10), nullable=True))


def downgrade() -> None:
    for c in ("cancel_fee_payer", "cancel_fee", "is_ready", "ready_at"):
        op.drop_column("merchant_deliveries", c)
