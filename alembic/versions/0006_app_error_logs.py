"""app error logs

Revision ID: 0006
Revises: 0005
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'app_error_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('device_id', sa.String(length=100), nullable=True),
        sa.Column('platform', sa.String(length=20), nullable=True),
        sa.Column('model', sa.String(length=120), nullable=True),
        sa.Column('os_name', sa.String(length=40), nullable=True),
        sa.Column('os_version', sa.String(length=40), nullable=True),
        sa.Column('app_version', sa.String(length=30), nullable=True),
        sa.Column('locale', sa.String(length=20), nullable=True),
        sa.Column('level', sa.String(length=10), nullable=False),
        sa.Column('source', sa.String(length=40), nullable=False),
        sa.Column('error_type', sa.String(length=120), nullable=True),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('stack', sa.Text(), nullable=True),
        sa.Column('context', sa.JSON(), nullable=True),
        sa.Column('occurred_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_app_error_logs_created_at', 'app_error_logs', ['created_at'])
    op.create_index('ix_app_error_logs_source', 'app_error_logs', ['source'])
    op.create_index('ix_app_error_level_created', 'app_error_logs', ['level', 'created_at'])
    op.create_index('ix_app_error_user_created', 'app_error_logs', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('app_error_logs')
