import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def memory_db():
    """In-memory SQLite connection — mirrors production: foreign keys + decltype parsing."""
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"
