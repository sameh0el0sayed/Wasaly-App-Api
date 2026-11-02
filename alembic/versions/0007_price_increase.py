"""price increase: raise by the customer and driver counter-offers

Revision ID: 0007
Revises: 0006
"""
from alembic import op
import sqlalchemy as sa


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('orders', sa.Column('price_increase_percent', sa.Integer(), server_default='0', nullable=False))
    op.add_column('orders', sa.Column('preferred_driver_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True))
    op.create_index('ix_orders_preferred_driver_id', 'orders', ['preferred_driver_id'])
    op.add_column('orders', sa.Column('choose_driver', sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column('orders', sa.Column('price_increase_amount', sa.Numeric(precision=12, scale=3), nullable=True))
    op.create_table(
        'counter_offers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.Integer(), nullable=False),
        sa.Column('offer_id', sa.Integer(), nullable=False),
        sa.Column('driver_id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('percent', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=8), server_default='COUNTER', nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('responded_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['order_id'], ['orders.id']),
        sa.ForeignKeyConstraint(['offer_id'], ['order_offers.id']),
        sa.ForeignKeyConstraint(['driver_id'], ['users.id']),
        sa.ForeignKeyConstraint(['customer_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('offer_id'),
    )
    op.create_index('ix_counter_offers_order_id', 'counter_offers', ['order_id'])
    op.create_index('ix_counter_offers_status', 'counter_offers', ['status'])


def downgrade() -> None:
    op.drop_table('counter_offers')
    op.drop_column('orders', 'price_increase_amount')
    op.drop_column('orders', 'choose_driver')
    op.drop_index('ix_orders_preferred_driver_id', table_name='orders')
    op.drop_column('orders', 'preferred_driver_id')
    op.drop_column('orders', 'price_increase_percent')
