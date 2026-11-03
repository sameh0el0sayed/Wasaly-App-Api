"""v15: one account, two roles (roles list + driver applications)

Revision ID: 0010
Revises: 0009
"""
import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("roles", sa.JSON(), nullable=True))
    op.create_table("driver_applications", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, unique=True),
                    sa.Column("status", sa.String(20), nullable=False, index=True), sa.Column("reason", sa.String(500)),
                    sa.Column("payload", sa.JSON()), sa.Column("created_at", sa.DateTime(timezone=True)),
                    sa.Column("decided_at", sa.DateTime(timezone=True)),
                    sa.Column("decided_by", sa.Integer(), sa.ForeignKey("users.id")))


def downgrade() -> None:
    op.drop_table("driver_applications")
    op.drop_column("users", "roles")
