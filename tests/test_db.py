import sqlite3

import pytest

from emission_tracker.config import PersonConfig
from emission_tracker.db import (
    cleanup_orphaned_snapshots,
    connect,
    init_schema,
    sync_team,
)


def test_init_schema_creates_all_tables(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    cursor = memory_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row["name"] for row in cursor.fetchall()]
    assert tables == [
        "hotkeys",
        "kas_distribution_lines",
        "kas_distributions",
        "neuron_snapshots",
        "persons",
        "settlement_lines",
        "settlements",
        "snapshots",
    ]


def test_settlements_fk_cascade_to_lines(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    # seed a snapshot to reference
    memory_db.execute(
        "INSERT INTO snapshots (id, taken_at, status) "
        "VALUES (1, CURRENT_TIMESTAMP, 'ok')"
    )
    memory_db.execute(
        "INSERT INTO settlements (id, settled_at, settled_through_snapshot_id, total_cumulative_rao) "
        "VALUES (1, CURRENT_TIMESTAMP, 1, 0)"
    )
    memory_db.execute(
        "INSERT INTO settlement_lines (settlement_id, hotkey_ss58, person_name, cumulative_rao) "
        "VALUES (1, '5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1', 'Alice', 1000)"
    )
    memory_db.commit()
    assert memory_db.execute("SELECT COUNT(*) FROM settlement_lines").fetchone()[0] == 1
    memory_db.execute("DELETE FROM settlements WHERE id = 1")
    memory_db.commit()
    assert memory_db.execute("SELECT COUNT(*) FROM settlement_lines").fetchone()[0] == 0


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


def test_snapshots_status_rejects_invalid_value(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO snapshots (taken_at, status) VALUES (CURRENT_TIMESTAMP, 'in-progress')"
        )


def test_neuron_snapshots_is_registered_rejects_non_boolean(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    # need a snapshot + hotkey first to satisfy FKs
    memory_db.execute(
        "INSERT INTO persons (name) VALUES ('A')"
    )
    memory_db.execute(
        "INSERT INTO hotkeys (ss58, person_id, subnet_id) VALUES "
        "('5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1', 1, 56)"
    )
    memory_db.execute(
        "INSERT INTO snapshots (id, taken_at, status) VALUES (1, CURRENT_TIMESTAMP, 'ok')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        memory_db.execute(
            "INSERT INTO neuron_snapshots (snapshot_id, hotkey_ss58, is_registered) "
            "VALUES (1, '5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1', 2)"
        )


def test_cleanup_orphaned_snapshots_marks_in_progress_failed(
    memory_db: sqlite3.Connection,
):
    init_schema(memory_db)
    memory_db.execute(
        "INSERT INTO snapshots (id, taken_at, status) "
        "VALUES (1, CURRENT_TIMESTAMP, 'in_progress'),"
        "       (2, CURRENT_TIMESTAMP, 'ok'),"
        "       (3, CURRENT_TIMESTAMP, 'in_progress'),"
        "       (4, CURRENT_TIMESTAMP, 'partial'),"
        "       (5, CURRENT_TIMESTAMP, 'failed')"
    )
    memory_db.commit()

    rowcount = cleanup_orphaned_snapshots(memory_db)
    assert rowcount == 2

    statuses = [
        r["status"]
        for r in memory_db.execute(
            "SELECT status FROM snapshots ORDER BY id"
        ).fetchall()
    ]
    assert statuses == ["failed", "ok", "failed", "partial", "failed"]


def test_cleanup_orphaned_snapshots_returns_zero_when_none(
    memory_db: sqlite3.Connection,
):
    init_schema(memory_db)
    memory_db.execute(
        "INSERT INTO snapshots (taken_at, status) VALUES "
        "(CURRENT_TIMESTAMP, 'ok'), (CURRENT_TIMESTAMP, 'failed')"
    )
    memory_db.commit()
    assert cleanup_orphaned_snapshots(memory_db) == 0


def test_cleanup_orphaned_snapshots_is_idempotent(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    memory_db.execute(
        "INSERT INTO snapshots (taken_at, status) VALUES "
        "(CURRENT_TIMESTAMP, 'in_progress')"
    )
    memory_db.commit()
    assert cleanup_orphaned_snapshots(memory_db) == 1
    # Second call: nothing left in_progress
    assert cleanup_orphaned_snapshots(memory_db) == 0
