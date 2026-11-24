"""Company accounts from Phoenix ID: corporates.phoenix_org_id and profile fields (additive)

Revision ID: 0039
Revises: 0038
"""
import sqlalchemy as sa

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("corporates", sa.Column("phoenix_org_id", sa.String(64), nullable=True))
    op.create_index("ix_corporates_phoenix_org_id", "corporates", ["phoenix_org_id"], unique=True)
    for c, n in (("name_ar", 160), ("pickup_address", 300), ("contact_person", 120), ("billing_email", 160)):
        op.add_column("corporates", sa.Column(c, sa.String(n), nullable=True))


def downgrade() -> None:
    for c in ("billing_email", "contact_person", "pickup_address", "name_ar"):
        op.drop_column("corporates", c)
    op.drop_index("ix_corporates_phoenix_org_id", "corporates")
    op.drop_column("corporates", "phoenix_org_id")
