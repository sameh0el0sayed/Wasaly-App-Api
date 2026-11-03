"""v14: translations dictionary + analytics events

Revision ID: 0009
Revises: 0008
"""
import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("cpr_issue_date", sa.Date(), nullable=True))
    op.add_column("users", sa.Column("cpr_expiry_date", sa.Date(), nullable=True))
    op.add_column("driver_profiles", sa.Column("license_issue_date", sa.Date(), nullable=True))
    op.add_column("notifications", sa.Column("data", sa.JSON(), nullable=True))
    op.create_table("translations", sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("key", sa.String(500), nullable=False, unique=True),
                    sa.Column("ar", sa.Text(), nullable=False, server_default=""),
                    sa.Column("en", sa.Text(), nullable=False, server_default=""),
                    sa.Column("updated_at", sa.DateTime(timezone=True)))
    op.create_table("app_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("user_id", sa.Integer()),
                    sa.Column("role", sa.String(10)), sa.Column("device_id", sa.String(100)),
                    sa.Column("session_id", sa.String(64), index=True), sa.Column("name", sa.String(60), nullable=False),
                    sa.Column("screen", sa.String(120)), sa.Column("props", sa.JSON()), sa.Column("app_version", sa.String(20)),
                    sa.Column("platform", sa.String(10)), sa.Column("client_ts", sa.DateTime(timezone=True)),
                    sa.Column("created_at", sa.DateTime(timezone=True)))
    op.create_index("ix_app_events_name_created", "app_events", ["name", "created_at"])
    op.create_index("ix_app_events_user_created", "app_events", ["user_id", "created_at"])


def downgrade() -> None:
    op.drop_table("app_events")
    op.drop_table("translations")
