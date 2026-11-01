"""profile corrections / changes without locking the account

Revision ID: 0005
Revises: 0004
"""
from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'profile_changes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=12), nullable=False),
        sa.Column('field', sa.String(length=40), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=True),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('requested_by', sa.Integer(), nullable=True),
        sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('submitted_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('reviewed_by', sa.Integer(), nullable=True),
        sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.ForeignKeyConstraint(['requested_by'], ['users.id']),
        sa.ForeignKeyConstraint(['reviewed_by'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_profile_changes_user_id', 'profile_changes', ['user_id'])
    op.create_index('ix_profile_changes_status', 'profile_changes', ['status'])
    op.add_column('driver_profiles', sa.Column('license_document_back', sa.String(length=255), nullable=True))
    op.add_column('driver_profiles', sa.Column('has_open_corrections', sa.Boolean(), server_default=sa.false(), nullable=False))


def downgrade() -> None:
    op.drop_column('driver_profiles', 'has_open_corrections')
    op.drop_column('driver_profiles', 'license_document_back')
    op.drop_index('ix_profile_changes_status', table_name='profile_changes')
    op.drop_index('ix_profile_changes_user_id', table_name='profile_changes')
    op.drop_table('profile_changes')
