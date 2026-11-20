"""Phoenix ID verified picture (short-lived URL, refreshed on every login)

Revision ID: 0033
Revises: 0032
"""
import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("kyc_picture", sa.String(1000), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "kyc_picture")
