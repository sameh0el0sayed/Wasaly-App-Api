"""v21: partner bank details + Tap payouts of statements

Revision ID: 0016
Revises: 0015
"""
import sqlalchemy as sa

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("partners", sa.Column("iban", sa.String(34), nullable=True))
    op.add_column("partners", sa.Column("bank_name", sa.String(120), nullable=True))
    op.add_column("partners", sa.Column("beneficiary_name", sa.String(120), nullable=True))
    op.add_column("partners", sa.Column("tap_destination_id", sa.String(80), nullable=True))
    op.add_column("partner_statements", sa.Column("pay_method", sa.String(10), nullable=True))
    op.create_table(
        "partner_payouts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("statement_id", sa.Integer(), sa.ForeignKey("partner_statements.id"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(12, 3), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="PENDING", index=True),
        sa.Column("provider", sa.String(10), nullable=False, server_default="TAP"),
        sa.Column("provider_ref", sa.String(120), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("partner_payouts")
    op.drop_column("partner_statements", "pay_method")
    for c in ("tap_destination_id", "beneficiary_name", "bank_name", "iban"):
        op.drop_column("partners", c)
