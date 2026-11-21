"""Unify the Phoenix ID button label (sign in or sign up) and bump the i18n version

Revision ID: 0035
Revises: 0034
"""
import sqlalchemy as sa

from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None

AR = "الدخول والتسجيل عبر Phoenix ID"
EN = "Sign in or sign up with Phoenix ID"


def upgrade() -> None:
    for key in ("v43.login.phoenix_id_button", "v42.phoenix_id.sign_in", "auth.phoenix_id_button"):
        op.execute(sa.text("UPDATE translations SET ar = :ar, en = :en WHERE key = :k").bindparams(ar=AR, en=EN, k=key))
    op.execute(sa.text("UPDATE app_settings SET value = CAST(CAST(NULLIF(value, '') AS INTEGER) + 1 AS TEXT) WHERE key = 'i18n_version'"))


def downgrade() -> None:
    pass
