"""v24: driver fraud flags

Revision ID: 0019
Revises: 0018
"""
import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fraud_flags",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("driver_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rule", sa.String(30), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False),
        sa.Column("summary", sa.String(300), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN"),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for c in ("driver_id", "rule", "status", "created_at"):
        op.create_index(f"ix_fraud_flags_{c}", "fraud_flags", [c])


def downgrade() -> None:
    op.drop_table("fraud_flags")
