"""Alembic environment: async engine, URL from TENANT_PROJECTS_MIGRATIONS_DATABASE_URL (the owner),
metadata of every generated model.
"""

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from tenant_projects.core.db import Base

# importing the model modules registers their tables on Base.metadata
from tenant_projects.modules.projects.models import Base as _base_projects  # noqa: F401
from tenant_projects.modules.tasks.models import Base as _base_tasks  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _url() -> str:
    owner = os.environ.get("TENANT_PROJECTS_MIGRATIONS_DATABASE_URL")
    return owner or os.environ.get("TENANT_PROJECTS_DATABASE_URL") or config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=None)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
