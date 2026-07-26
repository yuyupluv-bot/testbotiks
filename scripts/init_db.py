"""One-shot database initializer (schema + seed data).

Run this ONCE against your production PostgreSQL (Supabase/Neon) from any
machine that can reach the database (your laptop, bothost.ru, etc.).
Vercel is serverless + read-only, so you CANNOT run it there — point
DATABASE_URL at the same external database and run it locally instead.

    # 1. put the same DATABASE_URL / ADMIN_LOGIN / ADMIN_PASSWORD_HASH in .env
    # 2. install deps:  pip install -r requirements.txt
    # 3. run:
    python -m scripts.init_db

This creates every table defined in common/models.py and then seeds the admin
user, a default city, sample streets and default settings. It is idempotent:
running it again will not duplicate data, and it refreshes the admin password
hash from ADMIN_PASSWORD_HASH.

Equivalent to `alembic upgrade head && python -m scripts.seed`, but does not
require Alembic to be configured — handy for a first deploy.
"""
from __future__ import annotations

import sys

from common.config import config
from common.database import engine
from common.logger import get_logger
from common.models import Base
from scripts import seed

log = get_logger("scripts.init_db")


def run() -> None:
    if not config.ADMIN_PASSWORD_HASH:
        print(
            "⚠️  ADMIN_PASSWORD_HASH is empty. Generate one first:\n"
            "    python -m scripts.gen_password_hash 'your-password'\n"
            "then put it in .env (or the environment) and re-run."
        )
        sys.exit(1)

    print(f"➡️  Connecting to: {config.sqlalchemy_url().split('@')[-1]}")
    print("➡️  Creating tables (if they do not exist)…")
    Base.metadata.create_all(bind=engine)
    log.info("Schema created / verified")

    print("➡️  Seeding admin user, city, streets and settings…")
    seed.run()

    print(
        "✅ Database initialized. You can now log in to the admin panel with "
        f"login '{config.ADMIN_LOGIN}'."
    )


if __name__ == "__main__":
    run()
