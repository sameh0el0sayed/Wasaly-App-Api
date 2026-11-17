"""Partner portal: merchant members

Revision ID: 0028
Revises: 0027
"""
import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("merchant_members",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("merchant_id", sa.String(36), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("role", sa.String(10), nullable=False, server_default="VIEWER"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("merchant_id", "user_id", name="uq_merchant_member"))
    op.create_index("ix_merchant_members_merchant_id", "merchant_members", ["merchant_id"])
    op.create_index("ix_merchant_members_user_id", "merchant_members", ["user_id"])


def downgrade() -> None:
    op.drop_table("merchant_members")
