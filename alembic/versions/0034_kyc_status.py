"""Phoenix ID kyc_status / kyc_deadline on users

Revision ID: 0034
Revises: 0033
"""
import sqlalchemy as sa

from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("kyc_status", sa.String(30), nullable=True))
    op.add_column("users", sa.Column("kyc_deadline", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "kyc_deadline")
    op.drop_column("users", "kyc_status")
