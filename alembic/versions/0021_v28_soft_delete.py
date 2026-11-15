"""v28: soft delete for partners and service areas

Revision ID: 0021
Revises: 0020
"""
import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for t in ("partners", "service_areas"):
        op.add_column(t, sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
        op.create_index(f"ix_{t}_deleted_at", t, ["deleted_at"])


def downgrade() -> None:
    for t in ("partners", "service_areas"):
        op.drop_index(f"ix_{t}_deleted_at", table_name=t)
        op.drop_column(t, "deleted_at")
