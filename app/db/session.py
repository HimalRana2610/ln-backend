"""Async engine, session factory and the FastAPI session dependency."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.config import settings


def _engine_options() -> dict[str, Any]:
    """Pooling strategy, which differs sharply between the two deployments.

    **Long-running server** (local, Docker, Render): keep a pool. Connections
    are expensive to establish and the process outlives many requests.

    **Serverless** (Vercel functions): use ``NullPool``. A function instance may
    be frozen or discarded at any moment, so a pooled connection is likely to be
    dead when the instance thaws — and dozens of concurrent instances each
    holding a pool would exhaust a free-tier connection limit within seconds.
    The platform's own transaction pooler does the pooling instead.
    """
    common: dict[str, Any] = {
        "echo": settings.db_echo,
        "connect_args": settings.engine_connect_args,
    }

    if settings.db_serverless:
        return {**common, "poolclass": NullPool}

    return {
        **common,
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        # Verifies a connection before handing it out, so a link dropped by an
        # idle-timeout surfaces as a reconnect rather than a failed request.
        "pool_pre_ping": True,
    }


engine: AsyncEngine = create_async_engine(settings.sqlalchemy_url, **_engine_options())

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield a session that is committed on success and rolled back on error.

    Routes therefore never call ``commit()`` themselves - a request either
    succeeds as a whole or leaves the database untouched.
    """
    async with SessionFactory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()
