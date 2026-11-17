"""Unified services: service categories, vehicle categories, pricing profiles, provider categories, quotes

Revision ID: 0023
Revises: 0022
"""
import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table("service_categories",
        sa.Column("code", sa.String(20), primary_key=True), sa.Column("name_ar", sa.String(80), nullable=False),
        sa.Column("name_en", sa.String(80), nullable=False), sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"), sa.Column("config", sa.JSON(), nullable=True))
    op.create_table("vehicle_categories",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("service_code", sa.String(20), sa.ForeignKey("service_categories.code"), nullable=False),
        sa.Column("code", sa.String(30), nullable=False), sa.Column("name_ar", sa.String(80), nullable=False), sa.Column("name_en", sa.String(80), nullable=False),
        sa.Column("max_weight_kg", sa.Numeric(10, 2), nullable=True), sa.Column("max_length_cm", sa.Integer(), nullable=True),
        sa.Column("max_width_cm", sa.Integer(), nullable=True), sa.Column("max_height_cm", sa.Integer(), nullable=True),
        sa.Column("equipment", sa.JSON(), nullable=True), sa.Column("price_multiplier", sa.Numeric(6, 3), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("service_code", "code", name="uq_vehicle_category"))
    op.create_index("ix_vehicle_categories_service_code", "vehicle_categories", ["service_code"])
    op.create_table("pricing_profiles",
        sa.Column("service_code", sa.String(20), sa.ForeignKey("service_categories.code"), primary_key=True), sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("commission_type", sa.String(10), nullable=True), sa.Column("commission_value", sa.Numeric(10, 3), nullable=True),
        sa.Column("commission_mode", sa.String(10), nullable=True), sa.Column("updated_at", TS, nullable=False))
    op.create_table("provider_categories",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("driver_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("service_code", sa.String(20), sa.ForeignKey("service_categories.code"), nullable=False),
        sa.Column("vehicle_category_id", sa.Integer(), sa.ForeignKey("vehicle_categories.id"), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="PENDING_APPROVAL"), sa.Column("documents", sa.JSON(), nullable=True),
        sa.Column("note", sa.String(300), nullable=True), sa.Column("reviewed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", TS, nullable=False), sa.UniqueConstraint("driver_id", "service_code", name="uq_provider_service"))
    op.create_index("ix_provider_categories_driver_id", "provider_categories", ["driver_id"])
    op.create_index("ix_provider_categories_status", "provider_categories", ["status"])
    op.create_table("service_quotes",
        sa.Column("id", sa.Integer(), primary_key=True), sa.Column("customer_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("service_code", sa.String(20), sa.ForeignKey("service_categories.code"), nullable=False),
        sa.Column("vehicle_category_id", sa.Integer(), sa.ForeignKey("vehicle_categories.id"), nullable=True),
        sa.Column("request", sa.JSON(), nullable=False), sa.Column("breakdown", sa.JSON(), nullable=True), sa.Column("price", sa.Numeric(12, 3), nullable=True),
        sa.Column("currency", sa.String(3), nullable=False, server_default="BHD"), sa.Column("status", sa.String(16), nullable=False, server_default="QUOTED"),
        sa.Column("expires_at", TS, nullable=True), sa.Column("order_id", sa.Integer(), nullable=True), sa.Column("admin_note", sa.String(300), nullable=True),
        sa.Column("created_at", TS, nullable=False))
    op.create_index("ix_service_quotes_customer_id", "service_quotes", ["customer_id"])
    op.create_index("ix_service_quotes_status", "service_quotes", ["status"])
    op.add_column("orders", sa.Column("service_code", sa.String(20), nullable=True))
    op.add_column("orders", sa.Column("vehicle_category_id", sa.Integer(), sa.ForeignKey("vehicle_categories.id"), nullable=True))
    op.add_column("orders", sa.Column("quote_id", sa.Integer(), nullable=True))
    op.add_column("orders", sa.Column("agreed_price", sa.Numeric(12, 3), nullable=True))
    op.add_column("orders", sa.Column("service_details", sa.JSON(), nullable=True))
    op.create_index("ix_orders_service_code", "orders", ["service_code"])


def downgrade() -> None:
    op.drop_index("ix_orders_service_code", table_name="orders")
    for c in ("service_details", "agreed_price", "quote_id", "vehicle_category_id", "service_code"):
        op.drop_column("orders", c)
    for t in ("service_quotes", "provider_categories", "pricing_profiles", "vehicle_categories", "service_categories"):
        op.drop_table(t)
