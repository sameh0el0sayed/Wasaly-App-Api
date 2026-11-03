"""v16: passkeys (WebAuthn)

Revision ID: 0011
Revises: 0010
"""
import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("passkeys", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
                    sa.Column("credential_id", sa.String(512), nullable=False, unique=True),
                    sa.Column("public_key", sa.LargeBinary(), nullable=False), sa.Column("sign_count", sa.Integer(), server_default="0"),
                    sa.Column("transports", sa.JSON()), sa.Column("device_name", sa.String(100)),
                    sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("last_used_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.drop_table("passkeys")
