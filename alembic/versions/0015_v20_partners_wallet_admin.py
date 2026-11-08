"""v20: partners with monthly statements + admin wallet top-ups

Revision ID: 0015
Revises: 0014
"""
import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallet_entries", sa.Column("note", sa.Text(), nullable=True))
    op.add_column("wallet_entries", sa.Column("admin_id", sa.Integer(), nullable=True))
    op.create_table(
        "partners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "partner_statements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("partner_id", sa.Integer(), sa.ForeignKey("partners.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("gross_commission", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("referral_cost", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("base_amount", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("percent", sa.Numeric(5, 2), nullable=False),
        sa.Column("amount", sa.Numeric(12, 3), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING", index=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paid_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("paid_ref", sa.String(120), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("partner_id", "period", name="uq_partner_period"),
    )


def downgrade() -> None:
    op.drop_table("partner_statements")
    op.drop_table("partners")
    op.drop_column("wallet_entries", "admin_id")
    op.drop_column("wallet_entries", "note")
