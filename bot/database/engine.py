from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from bot.database.models import Base

_ASYNCPG_VALID_SSL_MODES = {
    "disable",
    "prefer",
    "allow",
    "require",
    "verify-ca",
    "verify-full",
}


def _normalize_postgres_url(database_url: str) -> tuple[str, dict[str, Any]]:
    """Normalize cloud PostgreSQL URLs for asyncpg and serverless poolers.

    Handles standard ``postgres://`` / ``postgresql://`` URLs provided by Neon,
    Supabase, Railway, etc., maps ``sslmode`` to asyncpg's ``ssl`` connect arg,
    and disables prepared statement caching for PgBouncer transaction poolers.
    """
    raw = database_url.strip()
    if raw.startswith("postgres://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgres://")
    elif raw.startswith("postgresql://"):
        raw = "postgresql+asyncpg://" + raw.removeprefix("postgresql://")

    connect_args: dict[str, Any] = {}
    if raw.startswith("postgresql+asyncpg://"):
        parsed = urlsplit(raw)
        if parsed.query:
            remaining_params: list[tuple[str, str]] = []
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                lower_key = key.lower()
                if lower_key == "sslmode":
                    lower_val = value.lower()
                    if lower_val == "disable":
                        connect_args["ssl"] = False
                    elif lower_val in _ASYNCPG_VALID_SSL_MODES:
                        connect_args["ssl"] = lower_val
                elif lower_key in {"channel_binding", "pgbouncer"}:
                    # Not supported as keyword arguments by asyncpg.connect()
                    continue
                else:
                    remaining_params.append((key, value))

            raw = urlunsplit(
                (
                    parsed.scheme,
                    parsed.netloc,
                    parsed.path,
                    urlencode(remaining_params),
                    parsed.fragment,
                )
            )

    return raw, connect_args


def create_db_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    """Create async SQLAlchemy engine configured for Serverless (NullPool + asyncpg).

    Disables both asyncpg and SQLAlchemy prepared statement caches for full
    compatibility with Supabase Transaction Pooler (port 6543 / PgBouncer).
    """
    normalized_url, url_connect_args = _normalize_postgres_url(database_url)
    return create_async_engine(
        normalized_url,
        echo=echo,
        poolclass=NullPool,
        connect_args={
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            **url_connect_args,
        },
    )


def create_session_maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_db(engine: AsyncEngine) -> None:
    """Create all database tables if they do not exist."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def close_db(engine: AsyncEngine) -> None:
    """Dispose database engine and release connections."""
    await engine.dispose()
