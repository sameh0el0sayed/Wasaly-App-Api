"""Tap fee breakdown on payments

Revision ID: 0029
Revises: 0028
"""
import sqlalchemy as sa
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments", sa.Column("fee_category", sa.String(10)))
    op.add_column("payments", sa.Column("fee_detail", sa.JSON()))
    op.add_column("payments", sa.Column("card_brand", sa.String(30)))


def downgrade() -> None:
    for c in ("card_brand", "fee_detail", "fee_category"):
        op.drop_column("payments", c)
