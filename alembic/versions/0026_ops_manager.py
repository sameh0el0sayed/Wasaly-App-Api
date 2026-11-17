"""AI operations manager: findings and reports

Revision ID: 0026
Revises: 0025
"""
import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("ops_findings",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("severity", sa.String(8), nullable=False, server_default="MEDIUM"), sa.Column("entity_type", sa.String(20), nullable=False),
        sa.Column("entity_id", sa.String(40), nullable=False), sa.Column("message", sa.String(400), nullable=False),
        sa.Column("suggestion", sa.String(600)), sa.Column("data", sa.JSON()),
        sa.Column("status", sa.String(10), nullable=False, server_default="OPEN"), sa.Column("created_at", TS, nullable=False),
        sa.Column("resolved_by", sa.Integer(), sa.ForeignKey("users.id")), sa.Column("resolved_at", TS))
    op.create_index("ix_ops_findings_kind", "ops_findings", ["kind"])
    op.create_index("ix_ops_findings_entity_id", "ops_findings", ["entity_id"])
    op.create_index("ix_ops_findings_status", "ops_findings", ["status"])
    op.create_table("ops_reports",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("period_start", TS, nullable=False), sa.Column("period_end", TS, nullable=False),
        sa.Column("metrics", sa.JSON(), nullable=False), sa.Column("text_ar", sa.Text(), nullable=False), sa.Column("created_at", TS, nullable=False))


def downgrade() -> None:
    op.drop_table("ops_reports")
    op.drop_table("ops_findings")
