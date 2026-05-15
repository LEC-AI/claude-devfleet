"""Shared pytest fixtures for DevFleet backend tests."""

import asyncio
import os
import sys
import tempfile

import pytest
import pytest_asyncio

# Ensure backend/ is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the whole test session."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def tmp_db(tmp_path, monkeypatch):
    """Initialise a fresh in-memory-style SQLite DB for each test."""
    import db as _db

    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(_db, "DB_PATH", db_path)
    await _db.init_db()
    yield db_path
