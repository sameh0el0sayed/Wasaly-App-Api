"""Merchant leads from the landing page (additive)

Revision ID: 0040
Revises: 0039
"""
import sqlalchemy as sa

from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "merchant_leads",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("business", sa.String(150), nullable=False),
        sa.Column("phone", sa.String(30), nullable=False),
        sa.Column("phone_norm", sa.String(30), nullable=False),
        sa.Column("area", sa.String(100), nullable=True),
        sa.Column("message", sa.Text, nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="NEW"),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column("ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_merchant_leads_phone_norm", "merchant_leads", ["phone_norm"])
    op.create_index("ix_merchant_leads_status", "merchant_leads", ["status"])
    op.create_index("ix_merchant_leads_created_at", "merchant_leads", ["created_at"])


def downgrade() -> None:
    op.drop_table("merchant_leads")
