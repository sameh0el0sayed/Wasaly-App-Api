"""Merchant integrations: kind, custom pricing, rate limit, extra keys, sub-merchants

Revision ID: 0025
Revises: 0024
"""
import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("merchants", sa.Column("kind", sa.String(12), nullable=False, server_default="MERCHANT"))
    op.add_column("merchants", sa.Column("pricing_mode", sa.String(10), nullable=False, server_default="STANDARD"))
    for c in ("fixed_fee", "base_fee", "per_km"):
        op.add_column("merchants", sa.Column(c, sa.Numeric(10, 3), nullable=True))
    op.add_column("merchants", sa.Column("rate_limit_per_min", sa.Integer(), nullable=False, server_default="600"))
    op.create_table("merchant_keys",
        sa.Column("id", sa.String(36), primary_key=True), sa.Column("merchant_id", sa.String(36), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False, unique=True), sa.Column("label", sa.String(60)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_at", TS, nullable=False))
    op.create_index("ix_merchant_keys_merchant_id", "merchant_keys", ["merchant_id"])
    op.create_index("ix_merchant_keys_key_hash", "merchant_keys", ["key_hash"])
    op.add_column("merchant_deliveries", sa.Column("sub_merchant_ref", sa.String(100)))
    op.add_column("merchant_deliveries", sa.Column("sub_merchant_name", sa.String(120)))
    op.create_index("ix_merchant_deliveries_sub_merchant_ref", "merchant_deliveries", ["sub_merchant_ref"])


def downgrade() -> None:
    op.drop_index("ix_merchant_deliveries_sub_merchant_ref", "merchant_deliveries")
    op.drop_column("merchant_deliveries", "sub_merchant_name")
    op.drop_column("merchant_deliveries", "sub_merchant_ref")
    op.drop_table("merchant_keys")
    for c in ("rate_limit_per_min", "per_km", "base_fee", "fixed_fee", "pricing_mode", "kind"):
        op.drop_column("merchants", c)
