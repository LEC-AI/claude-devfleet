import asyncio
import os
import sys

import pytest

# Backend modules import each other as top-level modules (`import db`)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point db.get_db() at a fresh SQLite file with the real schema applied."""
    path = str(tmp_path / "devfleet-test.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    asyncio.run(db.init_db())
    return path
