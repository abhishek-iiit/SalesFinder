"""Alembic environment (async). Uses a supplied connection if present, else its own engine."""

import asyncio
import os

from alembic import context
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import salesfinder.db.models  # noqa: F401  (register models on metadata)
from salesfinder.db.base import Base
from salesfinder.settings import get_settings

config = context.config
target_metadata = Base.metadata


def _do_run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_with_own_engine() -> None:
    url = (
        config.attributes.get("database_url")
        or os.environ.get("SALESFINDER_DATABASE_URL")
        or get_settings().database_url  # alembic CLI: fall back to .env
    )
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_do_run)
            await conn.commit()
    finally:
        await engine.dispose()


def run_migrations() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _do_run(connection)
    else:
        asyncio.run(_run_with_own_engine())


if context.is_offline_mode():
    raise RuntimeError("Offline migrations are not supported")
run_migrations()
