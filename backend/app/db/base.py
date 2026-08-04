"""SQLAlchemy declarative base + async engine/session factory."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    # Every `Mapped[datetime]` / `Mapped[datetime | None]` column in app/db/models.py
    # maps to this by default. Without it, SQLAlchemy infers a bare `DateTime()`
    # (TIMESTAMP WITHOUT TIME ZONE) from the plain `datetime` type annotation, which
    # mismatches the `timestamptz` columns the Alembic migration actually creates --
    # asyncpg then rejects timezone-aware Python datetimes (e.g. from
    # `datetime.now(timezone.utc)`) with "can't subtract offset-naive and
    # offset-aware datetimes" on insert. This one line fixes it repo-wide instead of
    # annotating every column individually.
    type_annotation_map = {datetime: DateTime(timezone=True)}


def make_engine():
    settings = get_settings()
    return create_async_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=settings.db_pool_pre_ping,
        # PgBouncer in transaction-pooling mode hands each transaction a different
        # backend connection, so asyncpg's server-side prepared-statement cache
        # (keyed to a single physical connection) goes stale mid-session and raises
        # "prepared statement ... does not exist". Disabling the cache is the
        # documented workaround when sitting behind PgBouncer transaction mode.
        connect_args={"statement_cache_size": 0},
    )


_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = make_engine()
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_db() -> AsyncSession:  # FastAPI dependency generator, see api/deps.py
    factory = get_session_factory()
    async with factory() as session:
        yield session
