"""Phoenix ID single sign-on: users.phoenix_sub

Revision ID: 0031
Revises: 0030
"""
import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("phoenix_sub", sa.String(64), nullable=True))
    op.create_index("ix_users_phoenix_sub", "users", ["phoenix_sub"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_users_phoenix_sub", table_name="users")
    op.drop_column("users", "phoenix_sub")
