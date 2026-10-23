"""Database engine and sessions."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from tollgate.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=True,  # survive a Postgres restart without failing the next request
        # Bound connecting as well as each query: with Postgres down, fail fast.
        connect_args={
            "command_timeout": settings.db_command_timeout_s,
            "timeout": settings.db_command_timeout_s,
        },
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
