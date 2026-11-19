"""Phoenix Command link + owner unified config

Revision ID: 0030
Revises: 0029
"""
import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "command_link",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key_id", sa.String(60), nullable=False, server_default="phoenix-command"),
        sa.Column("secret_encrypted", sa.Text()),
        sa.Column("allowed_ips", sa.Text()),
        sa.Column("command_url", sa.String(300), nullable=False, server_default="https://phoenixsystems.online/control-api"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "owner_config",
        sa.Column("key", sa.String(60), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False, server_default=""),
        sa.Column("is_secret", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("owner_config")
    op.drop_table("command_link")
