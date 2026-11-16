"""Merchant delivery API (Phoenix POS)

Revision ID: 0022
Revises: 0021
"""
import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "merchants",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("phone", sa.String(32), nullable=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("api_key_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("signing_secret_encrypted", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_merchants_api_key_hash", "merchants", ["api_key_hash"])
    op.create_table(
        "merchant_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("merchant_id", sa.String(36), sa.ForeignKey("merchants.id"), nullable=False),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("orders.id"), nullable=False, unique=True),
        sa.Column("external_ref", sa.String(100), nullable=False),
        sa.Column("external_number", sa.String(60), nullable=True),
        sa.Column("webhook_url", sa.String(500), nullable=True),
        sa.Column("items_summary", sa.String(500), nullable=True),
        sa.Column("order_total", sa.String(20), nullable=True),
        sa.Column("quoted_price", sa.String(20), nullable=True),
        sa.Column("last_status", sa.String(20), nullable=False, server_default="searching"),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_index("ix_merchant_deliveries_merchant_id", "merchant_deliveries", ["merchant_id"])
    op.create_index("ix_merchant_deliveries_external_ref", "merchant_deliveries", ["external_ref"])
    op.create_table(
        "merchant_webhook_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("delivery_id", sa.String(36), sa.ForeignKey("merchant_deliveries.id"), nullable=False),
        sa.Column("raw_status", sa.String(30), nullable=False),
        sa.Column("occurred_at", TS, nullable=False),
        sa.Column("state", sa.String(10), nullable=False, server_default="PENDING"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", TS, nullable=False),
        sa.Column("last_error", sa.String(200), nullable=True),
    )
    op.create_index("ix_merchant_webhook_events_delivery_id", "merchant_webhook_events", ["delivery_id"])
    op.create_index("ix_merchant_webhook_events_state", "merchant_webhook_events", ["state"])
    op.create_index("ix_merchant_webhook_events_next_attempt_at", "merchant_webhook_events", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("merchant_webhook_events")
    op.drop_table("merchant_deliveries")
    op.drop_table("merchants")
