import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.db import Base, get_db
from app.core.security import create_access_token
from app.main import app

# Use SQLite in-memory for tests (fast, no Docker needed)
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(test_engine, monkeypatch) -> AsyncGenerator[AsyncClient, None]:
    session_factory = async_sessionmaker(test_engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    # Patch FERNET_KEY so security functions work in tests
    monkeypatch.setattr("app.core.config.settings.FERNET_KEY", TEST_FERNET_KEY)
    monkeypatch.setattr("app.core.security._fernet", None)

    # httpx 0.28 dropped the `app=` shortcut — an explicit ASGI transport is
    # now the supported way to drive the app in-process.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def no_broker(monkeypatch):
    """
    Keep API tests off the message broker.

    ``POST /automation/run`` dispatches a Celery task; without this the test
    would block trying to reach Redis. Dispatch is recorded so tests can assert
    it happened.
    """
    from app.workers.tasks import execute_mcp_task

    dispatched: list[tuple] = []
    monkeypatch.setattr(
        execute_mcp_task,
        "delay",
        lambda *args, **kwargs: dispatched.append((args, kwargs)),
    )
    return dispatched


@pytest.fixture
def auth_headers() -> dict:
    token = create_access_token({"sub": "test-user-123", "email": "test@example.com"})
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def fernet_key() -> str:
    return TEST_FERNET_KEY
