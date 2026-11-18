"""Async engine / session factory and tiny KV helpers."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from memora.config import get_settings
from memora.db.models import KV

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _factory
    if _engine is None:
        _engine = create_async_engine(
            get_settings().database_url,
            pool_size=5,
            max_overflow=5,
            pool_pre_ping=True,
            pool_recycle=1800,
        )
        _factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _factory is not None
    return _factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Unit of work: commit on success, roll back on any exception."""
    async with session_factory()() as session:
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None


async def kv_get(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(KV, key)
    return row.value.get("v", default) if row else default


async def kv_set(session: AsyncSession, key: str, value: Any) -> None:
    stmt = pg_insert(KV).values(key=key, value={"v": value})
    stmt = stmt.on_conflict_do_update(index_elements=[KV.key], set_={"value": {"v": value}, "updated_at": func.now()})
    await session.execute(stmt)
