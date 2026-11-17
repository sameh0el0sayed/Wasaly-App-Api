"""Corporate accounts: corporates, members, orders.corporate_id

Revision ID: 0024
Revises: 0023
"""
import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("corporates",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("name", sa.String(160), nullable=False),
        sa.Column("cr_number", sa.String(40)), sa.Column("business_type", sa.String(40)), sa.Column("contact_phone", sa.String(32)),
        sa.Column("contact_email", sa.String(160)), sa.Column("discount_percent", sa.Numeric(5, 2), nullable=False, server_default="0"),
        sa.Column("monthly_limit", sa.Numeric(12, 3)), sa.Column("account_manager", sa.String(120)),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id")), sa.Column("created_at", TS, nullable=False))
    op.create_table("corporate_members",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("corporate_id", sa.Integer(), sa.ForeignKey("corporates.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False), sa.Column("role", sa.String(12), nullable=False, server_default="EMPLOYEE"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()), sa.Column("created_at", TS, nullable=False),
        sa.UniqueConstraint("corporate_id", "user_id", name="uq_corporate_member"))
    op.create_index("ix_corporate_members_corporate_id", "corporate_members", ["corporate_id"])
    op.create_index("ix_corporate_members_user_id", "corporate_members", ["user_id"])
    op.add_column("orders", sa.Column("corporate_id", sa.Integer(), nullable=True))
    op.create_index("ix_orders_corporate_id", "orders", ["corporate_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_corporate_id", "orders")
    op.drop_column("orders", "corporate_id")
    op.drop_table("corporate_members")
    op.drop_table("corporates")
