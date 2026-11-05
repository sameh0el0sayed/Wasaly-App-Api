"""WhatsApp reverse OTP verification sessions

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("phone_verified", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.create_table(
        "whatsapp_verifications",
        sa.Column("verification_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("otp_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("token_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("verification_id"),
    )
    op.create_index("ix_whatsapp_verifications_user_id", "whatsapp_verifications", ["user_id"])
    op.create_index("ix_whatsapp_verifications_phone", "whatsapp_verifications", ["phone"])
    op.create_index("ix_whatsapp_verifications_expires_at", "whatsapp_verifications", ["expires_at"])
    op.create_index("ix_whatsapp_verifications_status", "whatsapp_verifications", ["status"])
    op.create_index("ix_whatsapp_verifications_created_at", "whatsapp_verifications", ["created_at"])
    op.create_index(
        "ix_whatsapp_verifications_phone_status_expiry",
        "whatsapp_verifications", ["phone", "status", "expires_at"],
    )
    op.create_table(
        "whatsapp_webhook_events",
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("provider_message_id"),
    )


def downgrade() -> None:
    op.drop_table("whatsapp_webhook_events")
    op.drop_index("ix_whatsapp_verifications_phone_status_expiry", table_name="whatsapp_verifications")
    op.drop_index("ix_whatsapp_verifications_created_at", table_name="whatsapp_verifications")
    op.drop_index("ix_whatsapp_verifications_status", table_name="whatsapp_verifications")
    op.drop_index("ix_whatsapp_verifications_expires_at", table_name="whatsapp_verifications")
    op.drop_index("ix_whatsapp_verifications_phone", table_name="whatsapp_verifications")
    op.drop_index("ix_whatsapp_verifications_user_id", table_name="whatsapp_verifications")
    op.drop_table("whatsapp_verifications")
    op.drop_column("users", "phone_verified")
