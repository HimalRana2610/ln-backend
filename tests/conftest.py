"""Test fixtures.

Each test runs against a real Postgres (the same engine as production - schema
differences between a SQLite test double and Postgres are exactly where bugs
hide) inside a transaction that is rolled back afterwards, so tests never see
each other's rows and can run in any order.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

os.environ.setdefault("ENVIRONMENT", "test")

# Tests drive note generation explicitly by calling `NoteService.process`, so
# the inline background drain must be off. httpx's ASGI transport *does* run
# background tasks, and leaving it on made every note-creating test wait out the
# drain's retry backoff — a 5-second suite became 54 seconds.
os.environ.setdefault("NOTES_INLINE_WORKER", "false")

from app.core.config import settings
from app.db.base import Base
from app.db.session import get_db
from app.main import create_app

TEST_DATABASE_URL = settings.sqlalchemy_url + "_test"


@pytest.fixture(scope="session")
async def engine() -> AsyncGenerator:
    """Create the test database schema once per run."""
    admin = create_async_engine(settings.sqlalchemy_url, isolation_level="AUTOCOMMIT")
    db_name = TEST_DATABASE_URL.rsplit("/", 1)[-1]
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await conn.exec_driver_sql(f'CREATE DATABASE "{db_name}"')
    await admin.dispose()

    test_engine = create_async_engine(TEST_DATABASE_URL)
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield test_engine
    await test_engine.dispose()


@pytest.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    """A session bound to a transaction that is rolled back after each test."""
    connection = await engine.connect()
    transaction = await connection.begin()
    # join_transaction_mode="create_savepoint" is what lets application code
    # call `commit()` without destroying test isolation: the commit releases a
    # SAVEPOINT rather than the outer transaction, so the rollback below still
    # undoes everything. NoteService.create commits deliberately — see the
    # comment there — and without this every note it creates would survive into
    # the next test.
    session = async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )()

    yield session

    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """HTTP client whose requests share the test's transaction."""
    app = create_app()

    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        # No commit here: the outer transaction is rolled back by db_session.
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as http_client:
        yield http_client

    app.dependency_overrides.clear()


@pytest.fixture
def user_payload() -> dict[str, str]:
    return {
        "email": "ada@example.edu",
        "full_name": "Ada Lovelace",
        "password": "correct-horse-battery",
        "institute": "Analytical Engine Institute",
    }
