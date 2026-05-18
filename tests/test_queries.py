import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.queries import (
    current_registration_status,
    dashboard_summary,
    hotkey_series,
    latest_snapshot,
    person_series,
)


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
HK_I1 = "5BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB1"


@pytest.fixture
def seeded_db(memory_db: sqlite3.Connection) -> sqlite3.Connection:
    init_schema(memory_db)
    sync_team(
        memory_db,
        [
            PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2]),
            PersonConfig(name="Bob", hotkeys=[HK_I1]),
        ],
        subnet_id=56,
    )
    # 3 snapshots, ascending time
    base = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)
    for i, dt in enumerate([base, base + timedelta(minutes=72), base + timedelta(minutes=144)]):
        memory_db.execute(
            "INSERT INTO snapshots (id, taken_at, block_number, status) VALUES (?, ?, ?, 'ok')",
            (i + 1, dt, 1000 + i),
        )
        # Alice HK1: 1.0, 2.0, 3.0 ; HK2: 0.5, 0.5, 0.5 ; Bob: 0.1, 0.2, 0.3
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 10, ?, 1)",
            (i + 1, HK_F1, [1.0, 2.0, 3.0][i]),
        )
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 11, ?, 1)",
            (i + 1, HK_F2, 0.5),
        )
        memory_db.execute(
            "INSERT INTO neuron_snapshots VALUES (?, ?, 12, ?, 1)",
            (i + 1, HK_I1, [0.1, 0.2, 0.3][i]),
        )
    memory_db.commit()
    return memory_db


def test_dashboard_summary_all_time(seeded_db: sqlite3.Connection):
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(7.5)  # 1+2+3 + 0.5*3
    assert by_name["Bob"]["cumulative"] == pytest.approx(0.6)  # 0.1+0.2+0.3
    # ordering: highest first
    assert rows[0]["name"] == "Alice"


def test_dashboard_summary_with_range_filter(seeded_db: sqlite3.Connection):
    # Range covering only snapshots 2 and 3
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2026, 5, 17, 13, 0, tzinfo=timezone.utc),
        to_dt=datetime(2026, 5, 17, 15, 0, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(6.0)  # 2+3 + 0.5+0.5
    assert by_name["Bob"]["cumulative"] == pytest.approx(0.5)  # 0.2+0.3


def test_dashboard_summary_excludes_failed_snapshots(seeded_db: sqlite3.Connection):
    seeded_db.execute("UPDATE snapshots SET status = 'failed' WHERE id = 1")
    seeded_db.commit()
    rows = dashboard_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_name = {r["name"]: r for r in rows}
    assert by_name["Alice"]["cumulative"] == pytest.approx(6.0)


def test_hotkey_series_returns_cumulative_running_sum(seeded_db: sqlite3.Connection):
    series = hotkey_series(
        seeded_db,
        hotkey=HK_F1,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    cumulatives = [s["cumulative"] for s in series]
    assert cumulatives == [pytest.approx(1.0), pytest.approx(3.0), pytest.approx(6.0)]


def test_person_series_aggregates_hotkeys(seeded_db: sqlite3.Connection):
    series = person_series(
        seeded_db,
        name="Alice",
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    # at each snapshot, total emission = HK_F1 + HK_F2
    per_snap = [s["per_snapshot_emission"] for s in series]
    assert per_snap == [pytest.approx(1.5), pytest.approx(2.5), pytest.approx(3.5)]
    cum = [s["cumulative"] for s in series]
    assert cum == [pytest.approx(1.5), pytest.approx(4.0), pytest.approx(7.5)]


def test_latest_snapshot_returns_most_recent_ok(seeded_db: sqlite3.Connection):
    snap = latest_snapshot(seeded_db)
    assert snap["id"] == 3
    assert snap["status"] == "ok"


def test_current_registration_status_all_active(seeded_db: sqlite3.Connection):
    status = current_registration_status(seeded_db)
    assert status["Alice"] == {"active": 2, "total": 2, "deregistered_hotkeys": []}
    assert status["Bob"] == {"active": 1, "total": 1, "deregistered_hotkeys": []}


def test_current_registration_status_detects_deregistered(seeded_db: sqlite3.Connection):
    # Simulate HK_F2 getting deregistered in the latest snapshot
    seeded_db.execute(
        "UPDATE neuron_snapshots SET is_registered = 0, uid = NULL, emission = NULL "
        "WHERE hotkey_ss58 = ? AND snapshot_id = 3",
        (HK_F2,),
    )
    seeded_db.commit()

    status = current_registration_status(seeded_db)
    assert status["Alice"]["active"] == 1
    assert status["Alice"]["total"] == 2
    assert status["Alice"]["deregistered_hotkeys"] == [HK_F2]
    # Bob unaffected
    assert status["Bob"]["active"] == 1


def test_current_registration_status_missing_row_counts_as_deregistered(
    seeded_db: sqlite3.Connection,
):
    # Worker failure: no row for HK_I1 in latest snapshot (LEFT JOIN gives NULL)
    seeded_db.execute(
        "DELETE FROM neuron_snapshots WHERE hotkey_ss58 = ? AND snapshot_id = 3",
        (HK_I1,),
    )
    seeded_db.commit()

    status = current_registration_status(seeded_db)
    assert status["Bob"]["active"] == 0
    assert status["Bob"]["total"] == 1
    assert HK_I1 in status["Bob"]["deregistered_hotkeys"]


def test_current_registration_status_no_snapshots_yet(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="X", hotkeys=[HK_F1])],
        subnet_id=56,
    )
    status = current_registration_status(memory_db)
    # No snapshots → all hotkeys count as deregistered (visibility)
    assert status["X"]["active"] == 0
    assert status["X"]["total"] == 1
    assert status["X"]["deregistered_hotkeys"] == [HK_F1]
