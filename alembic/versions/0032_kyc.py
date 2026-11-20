"""Phoenix ID KYC claims on users

Revision ID: 0032
Revises: 0031
"""
import sqlalchemy as sa

from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("kyc_level", sa.String(20), nullable=False, server_default="none"))
    op.add_column("users", sa.Column("kyc_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("org_kyc_level", sa.String(20), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "org_kyc_level")
    op.drop_column("users", "kyc_verified_at")
    op.drop_column("users", "kyc_level")
