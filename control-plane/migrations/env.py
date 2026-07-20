"""Alembic environment for VoxBench control-plane migrations."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from voxbench.control_plane.models import Base

config = context.config

database_url = os.environ.get("VOXBENCH_DATABASE_URL")
if database_url is not None:
    try:
        parsed_database_url = make_url(database_url)
    except ArgumentError:
        raise RuntimeError("database migration URL is invalid") from None
    if parsed_database_url.drivername != "postgresql+psycopg" or not parsed_database_url.database:
        raise RuntimeError("database migration URL is invalid")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
