"""Unify the Phoenix ID button label (sign in or sign up) and bump the i18n version

Revision ID: 0035
Revises: 0034
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

AR = "الدخول والتسجيل عبر Phoenix ID"
EN = "Sign in or sign up with Phoenix ID"


def upgrade() -> None:
    conn = op.get_bind()
    for key in ("v43.login.phoenix_id_button", "v42.phoenix_id.sign_in", "auth.phoenix_id_button"):
        conn.exec_driver_sql("UPDATE translations SET ar = %(ar)s, en = %(en)s WHERE key = %(k)s", {"ar": AR, "en": EN, "k": key})
    conn.exec_driver_sql(
        "UPDATE app_settings SET value = (CAST(NULLIF(value, '') AS INTEGER) + 1)::text WHERE key = 'i18n_version'")


def downgrade() -> None:
    pass
