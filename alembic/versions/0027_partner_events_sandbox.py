"""Partner webhooks for quotes, sandbox keys

Revision ID: 0027
Revises: 0026
"""
import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("merchant_webhook_events", "delivery_id", existing_type=sa.String(36), nullable=True)
    op.add_column("merchant_webhook_events", sa.Column("merchant_id", sa.String(36)))
    op.add_column("merchant_webhook_events", sa.Column("quote_id", sa.Integer()))
    op.add_column("merchant_webhook_events", sa.Column("webhook_url", sa.String(500)))
    op.add_column("merchant_keys", sa.Column("is_sandbox", sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    op.drop_column("merchant_keys", "is_sandbox")
    for c in ("webhook_url", "quote_id", "merchant_id"):
        op.drop_column("merchant_webhook_events", c)
    op.alter_column("merchant_webhook_events", "delivery_id", existing_type=sa.String(36), nullable=False)
