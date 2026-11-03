"""v19: referral code as a trip discount + wallet applied to payments

Revision ID: 0012
Revises: 0011
"""
import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("promo_fixed", sa.Numeric(12, 3), nullable=True))
    op.add_column("orders", sa.Column("referral_id", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("wallet_amount", sa.Numeric(12, 3), server_default="0", nullable=False))


def downgrade() -> None:
    op.drop_column("payments", "wallet_amount")
    op.drop_column("orders", "referral_id")
    op.drop_column("orders", "promo_fixed")
