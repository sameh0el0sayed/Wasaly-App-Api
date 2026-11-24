"""Vehicle classes: vehicles.vehicle_class/seats, orders.vehicle_class (additive)

Revision ID: 0038
Revises: 0037
"""
import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vehicles", sa.Column("vehicle_class", sa.String(20), nullable=False, server_default="STANDARD"))
    op.add_column("vehicles", sa.Column("seats", sa.Integer, nullable=True))
    op.add_column("orders", sa.Column("vehicle_class", sa.String(20), nullable=False, server_default="STANDARD"))


def downgrade() -> None:
    op.drop_column("orders", "vehicle_class")
    op.drop_column("vehicles", "seats")
    op.drop_column("vehicles", "vehicle_class")
