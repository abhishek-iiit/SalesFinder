"""Programmatic Alembic helpers; independent of the current working directory."""

from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _config(sync_conn: Connection) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = sync_conn
    return cfg


async def _run[T](database_url: str, fn: Callable[[Connection], T]) -> T:
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as conn:
            return await conn.run_sync(fn)
    finally:
        await engine.dispose()


async def upgrade_to_head(database_url: str) -> None:
    def fn(sync_conn: Connection) -> None:
        command.upgrade(_config(sync_conn), "head")

    await _run(database_url, fn)


async def downgrade_to(database_url: str, revision: str) -> None:
    def fn(sync_conn: Connection) -> None:
        command.downgrade(_config(sync_conn), revision)

    await _run(database_url, fn)


async def current_and_head(database_url: str) -> tuple[str | None, str]:
    def fn(sync_conn: Connection) -> tuple[str | None, str]:
        current = MigrationContext.configure(sync_conn).get_current_revision()
        head = ScriptDirectory.from_config(_config(sync_conn)).get_current_head()
        if head is None:
            raise RuntimeError("No Alembic head revision found")
        return current, head

    return await _run(database_url, fn)
