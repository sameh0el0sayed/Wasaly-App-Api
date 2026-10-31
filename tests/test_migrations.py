import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(db, *args):
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def test_migrations_apply_and_match_models(tmp_path):
    db = tmp_path / "m.db"
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    chk = _alembic(db, "check")  # fails when a model changed without a new migration
    assert chk.returncode == 0, chk.stdout + chk.stderr
