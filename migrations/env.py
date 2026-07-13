"""Alembic migration environment for PDF Bridge."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from pdf_bridge.core.config import get_settings
from pdf_bridge.persistence import models  # noqa: F401 -- registers mapped tables
from pdf_bridge.persistence.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_url() -> str:
    """Resolve the URL from -x, the environment, or validated app settings."""

    command_line_url = context.get_x_argument(as_dictionary=True).get("database_url")
    url = command_line_url or os.getenv("PDF_BRIDGE_DATABASE_URL")
    if not url:
        url = get_settings().database_url
    return url


def run_migrations_offline() -> None:
    """Run migrations without creating a live database connection."""

    context.configure(
        url=database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations through an engine configured from the resolved URL."""

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = database_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
