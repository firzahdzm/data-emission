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


@pytest.fixture(autouse=True)
def _no_chain_pacing(monkeypatch):
    """Every chain call waits 3s behind the last one in production, so
    the endpoint does not see a burst. Tests must not pay that: the
    pty-driven ones would spend minutes sleeping, and what the pacer
    does is verified directly in its own tests."""
    from emission_tracker.signer import btcli

    monkeypatch.setattr(btcli, "MIN_CHAIN_GAP_SECONDS", 0.0)
