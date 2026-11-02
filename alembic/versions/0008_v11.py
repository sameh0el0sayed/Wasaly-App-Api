"""v11: SOS, trip share, stops, referrals + wallet, scheduled trips, cancel fees, driver goal

Revision ID: 0008
Revises: 0007
"""
import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def _t(name, *cols):
    op.create_table(name, sa.Column("id", sa.Integer(), primary_key=True), *cols)


def upgrade() -> None:
    op.add_column("orders", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_orders_scheduled_at", "orders", ["scheduled_at"])
    op.add_column("orders", sa.Column("scheduled_fee", sa.Numeric(12, 3), server_default="0", nullable=False))
    op.add_column("orders", sa.Column("cancel_fee", sa.Numeric(12, 3), nullable=True))
    op.add_column("orders", sa.Column("cancel_fee_payer", sa.String(10), nullable=True))
    op.add_column("orders", sa.Column("stops_fee", sa.Numeric(12, 3), server_default="0", nullable=False))
    op.add_column("users", sa.Column("referral_code", sa.String(16), nullable=True))
    op.create_index("ix_users_referral_code", "users", ["referral_code"], unique=True)
    op.add_column("users", sa.Column("wallet_balance", sa.Numeric(12, 3), server_default="0", nullable=False))
    op.add_column("driver_profiles", sa.Column("daily_goal", sa.Numeric(12, 3), server_default="0", nullable=False))
    uid = lambda: sa.ForeignKey("users.id")  # noqa: E731
    oid = lambda: sa.ForeignKey("orders.id")  # noqa: E731
    _t("sos_alerts", sa.Column("order_id", sa.Integer(), oid(), index=True), sa.Column("raised_by_user_id", sa.Integer(), uid(), index=True),
       sa.Column("role", sa.String(10)), sa.Column("lat", sa.Float()), sa.Column("lng", sa.Float()), sa.Column("note", sa.String(500)),
       sa.Column("status", sa.String(10), index=True), sa.Column("created_at", sa.DateTime(timezone=True)),
       sa.Column("escalated_at", sa.DateTime(timezone=True)), sa.Column("resolved_by", sa.Integer(), uid()),
       sa.Column("resolved_at", sa.DateTime(timezone=True)), sa.Column("resolution", sa.String(500)))
    _t("trip_shares", sa.Column("order_id", sa.Integer(), oid(), index=True), sa.Column("token", sa.String(48), unique=True, index=True),
       sa.Column("revoked", sa.Boolean(), server_default=sa.false()), sa.Column("created_at", sa.DateTime(timezone=True)))
    _t("order_stops", sa.Column("order_id", sa.Integer(), oid(), index=True), sa.Column("seq", sa.Integer()), sa.Column("address", sa.String(300)),
       sa.Column("lat", sa.Float()), sa.Column("lng", sa.Float()), sa.Column("note", sa.String(300)), sa.Column("status", sa.String(10)),
       sa.Column("fee", sa.Numeric(12, 3), server_default="0"), sa.Column("added_at", sa.DateTime(timezone=True)))
    _t("referrals", sa.Column("inviter_id", sa.Integer(), uid(), index=True), sa.Column("invitee_id", sa.Integer(), uid(), unique=True),
       sa.Column("device_id", sa.String(100), index=True), sa.Column("status", sa.String(16)),
       sa.Column("reward_inviter", sa.Numeric(12, 3), server_default="0"), sa.Column("reward_invitee", sa.Numeric(12, 3), server_default="0"),
       sa.Column("created_at", sa.DateTime(timezone=True)), sa.Column("rewarded_at", sa.DateTime(timezone=True)))
    _t("wallet_entries", sa.Column("user_id", sa.Integer(), uid(), index=True), sa.Column("amount", sa.Numeric(12, 3)),
       sa.Column("kind", sa.String(20)), sa.Column("ref_id", sa.Integer()), sa.Column("created_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    for t in ("wallet_entries", "referrals", "order_stops", "trip_shares", "sos_alerts"):
        op.drop_table(t)
    op.drop_column("driver_profiles", "daily_goal")
    op.drop_column("users", "wallet_balance")
    op.drop_index("ix_users_referral_code", table_name="users")
    op.drop_column("users", "referral_code")
    for c in ("stops_fee", "cancel_fee_payer", "cancel_fee", "scheduled_fee"):
        op.drop_column("orders", c)
    op.drop_index("ix_orders_scheduled_at", table_name="orders")
    op.drop_column("orders", "scheduled_at")
