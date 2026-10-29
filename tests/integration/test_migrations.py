"""The migrations: they round-trip, and they build exactly the schema the ORM
models describe."""

import asyncio
import os

import asyncpg
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.helpers import ROOT
from tollgate.db.models import Base

SCRATCH_DB = "migration_check"


def _admin_dsn() -> str:
    return (
        make_url(os.environ["DATABASE_URL"])
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )


async def _admin(sql: str) -> None:
    conn = await asyncpg.connect(_admin_dsn())
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


async def _inspect(url: str) -> tuple[list[str], list[object]]:
    def run(connection: Connection) -> tuple[list[str], list[object]]:
        tables = inspect(connection).get_table_names()
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        return tables, diff

    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(run)
    finally:
        await engine.dispose()


def test_migrations_round_trip_and_match_the_models() -> None:
    url = make_url(os.environ["DATABASE_URL"]).set(database=SCRATCH_DB)
    url_text = url.render_as_string(hide_password=False)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url_text.replace("%", "%%"))
    asyncio.run(_admin(f"CREATE DATABASE {SCRATCH_DB}"))
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "base")
        tables, _ = asyncio.run(_inspect(url_text))
        assert tables == ["alembic_version"]  # nothing of ours left behind

        command.upgrade(config, "head")
        tables, diff = asyncio.run(_inspect(url_text))
        assert set(tables) == {"alembic_version", "tenants", "api_keys", "usage_events"}
        assert diff == []  # the migrated schema is exactly the models'
    finally:
        asyncio.run(_admin(f"DROP DATABASE {SCRATCH_DB} WITH (FORCE)"))
