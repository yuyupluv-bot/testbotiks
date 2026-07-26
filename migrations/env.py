"""Alembic environment.

The database URL is taken from the app config (DATABASE_URL env var), and
target metadata is the SQLAlchemy Base used by the whole project, so
autogenerate works out of the box.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make the project root importable.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.config import config as app_config  # noqa: E402
from common.models import Base  # noqa: E402

config = context.config
config.set_main_option("sqlalchemy.url", app_config.sqlalchemy_url())

if config.config_file_name is not None:
    # Do not disable bot/common loggers that were created before Alembic was
    # invoked programmatically. Otherwise BotHost shows two Alembic lines and
    # hides every subsequent startup/Long Poll error.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=app_config.sqlalchemy_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = app_config.sqlalchemy_url()
    connectable = engine_from_config(
        section, prefix="sqlalchemy.", poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
