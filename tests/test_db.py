import sqlite3

import pytest

from emission_tracker.config import DatabaseConfig, PersonConfig
from emission_tracker.db import connect, init_schema, sync_team


def test_init_schema_creates_all_tables(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    cursor = memory_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    assert tables == ["hotkeys", "neuron_snapshots", "persons", "snapshots"]


def test_init_schema_is_idempotent(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    init_schema(memory_db)  # second call must not raise


def test_connect_enables_foreign_keys(tmp_db_path):
    with connect(str(tmp_db_path)) as conn:
        cursor = conn.execute("PRAGMA foreign_keys")
        assert cursor.fetchone()[0] == 1


def test_sync_team_upserts_persons_and_hotkeys(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    team = [
        PersonConfig(
            name="Alice",
            hotkeys=[
                "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
                "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
            ],
        ),
    ]
    sync_team(memory_db, team, subnet_id=56)

    persons = list(memory_db.execute("SELECT name FROM persons"))
    assert [p["name"] for p in persons] == ["Alice"]

    hotkeys = list(
        memory_db.execute(
            "SELECT ss58, subnet_id FROM hotkeys ORDER BY ss58"
        )
    )
    assert {h["ss58"] for h in hotkeys} == {
        "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1",
        "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2",
    }
    assert all(h["subnet_id"] == 56 for h in hotkeys)


def test_sync_team_is_idempotent(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    team = [
        PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        ),
    ]
    sync_team(memory_db, team, subnet_id=56)
    sync_team(memory_db, team, subnet_id=56)
    count = memory_db.execute("SELECT COUNT(*) AS n FROM persons").fetchone()["n"]
    assert count == 1


def test_sync_team_preserves_removed_hotkeys(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"],
        )],
        subnet_id=56,
    )
    # second sync with no hotkeys for Alice
    sync_team(
        memory_db,
        [PersonConfig(
            name="Alice",
            hotkeys=["5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"],
        )],
        subnet_id=56,
    )
    rows = memory_db.execute("SELECT ss58 FROM hotkeys ORDER BY ss58").fetchall()
    assert len(rows) == 2  # both old and new preserved
