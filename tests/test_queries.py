import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.queries import (
    captures_table,
    create_settlement,
    current_registration_status,
    dashboard_hotkey_summary,
    dashboard_summary,
    delete_settlement,
    hotkey_series,
    kas_totals,
    last_settlement,
    last_settlement_snapshot_id,
    latest_snapshot,
    list_settlements,
    person_series,
    set_settlement_distribution,
    settlement_detail,
    snapshot_history,
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


def test_dashboard_hotkey_summary_all_time(seeded_db: sqlite3.Connection):
    rows = dashboard_hotkey_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    # 3 hotkeys total (HK_F1, HK_F2, HK_I1)
    assert len(rows) == 3

    by_hk = {r["hotkey"]: r for r in rows}
    # Cumulatives: F1=6.0 (1+2+3), F2=1.5 (0.5*3), I1=0.6 (0.1+0.2+0.3)
    assert by_hk[HK_F1]["cumulative"] == pytest.approx(6.0)
    assert by_hk[HK_F1]["name"] == "Alice"
    assert by_hk[HK_F1]["is_registered"] == 1
    assert by_hk[HK_F2]["cumulative"] == pytest.approx(1.5)
    assert by_hk[HK_I1]["cumulative"] == pytest.approx(0.6)

    # Order: cumulative DESC
    assert rows[0]["hotkey"] == HK_F1   # 6.0
    assert rows[1]["hotkey"] == HK_F2   # 1.5
    assert rows[2]["hotkey"] == HK_I1   # 0.6

    # last_refresh: all hotkeys were in snapshot 3 → taken_at of snapshot 3
    # (base + 144 min = 2026-05-17 14:24, ISO 8601 with T separator)
    assert rows[0]["last_refresh"].startswith("2026-05-17T14:24:")


def test_dashboard_hotkey_summary_range_filter(seeded_db: sqlite3.Connection):
    # Only snapshots 2 and 3 in range
    rows = dashboard_hotkey_summary(
        seeded_db,
        from_dt=datetime(2026, 5, 17, 13, 0, tzinfo=timezone.utc),
        to_dt=datetime(2026, 5, 17, 15, 0, tzinfo=timezone.utc),
    )
    by_hk = {r["hotkey"]: r for r in rows}
    assert by_hk[HK_F1]["cumulative"] == pytest.approx(5.0)  # 2+3
    assert by_hk[HK_F2]["cumulative"] == pytest.approx(1.0)  # 0.5+0.5
    assert by_hk[HK_I1]["cumulative"] == pytest.approx(0.5)  # 0.2+0.3


def test_dashboard_hotkey_summary_detects_deregistered_in_latest(
    seeded_db: sqlite3.Connection,
):
    # Mark HK_F2 deregistered in latest snapshot (id=3)
    seeded_db.execute(
        "UPDATE neuron_snapshots SET is_registered = 0, uid = NULL, emission = NULL "
        "WHERE hotkey_ss58 = ? AND snapshot_id = 3",
        (HK_F2,),
    )
    seeded_db.commit()
    rows = dashboard_hotkey_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_hk = {r["hotkey"]: r for r in rows}
    assert by_hk[HK_F2]["is_registered"] == 0
    assert by_hk[HK_F1]["is_registered"] == 1


def test_captures_table_ascending_order_with_emissions(seeded_db: sqlite3.Connection):
    result = captures_table(seeded_db, limit=20)
    # 3 snapshots in seed, ascending
    assert len(result["snapshots"]) == 3
    assert result["snapshots"][0]["id"] == 1
    assert result["snapshots"][-1]["id"] == 3

    # 3 hotkeys total
    by_hk = {r["hotkey"]: r for r in result["rows"]}
    assert set(by_hk.keys()) == {HK_F1, HK_F2, HK_I1}

    # HK_F1 cells across snapshots 1,2,3: 1.0, 2.0, 3.0
    cells = by_hk[HK_F1]["cells"]
    assert [c["emission"] for c in cells] == [1.0, 2.0, 3.0]
    assert all(c["is_registered"] == 1 for c in cells)


def test_captures_table_respects_limit(seeded_db: sqlite3.Connection):
    result = captures_table(seeded_db, limit=2)
    assert len(result["snapshots"]) == 2
    # Most recent 2 in ascending order: ids 2, 3
    assert [s["id"] for s in result["snapshots"]] == [2, 3]
    # Cells per row also length 2
    for r in result["rows"]:
        assert len(r["cells"]) == 2


def test_captures_table_handles_missing_cells(seeded_db: sqlite3.Connection):
    # Delete HK_I1's row in snapshot 2 → that cell should be None
    seeded_db.execute(
        "DELETE FROM neuron_snapshots WHERE hotkey_ss58 = ? AND snapshot_id = 2",
        (HK_I1,),
    )
    seeded_db.commit()
    result = captures_table(seeded_db, limit=20)
    by_hk = {r["hotkey"]: r for r in result["rows"]}
    # cells for HK_I1 across snapshots 1,2,3: filled, None, filled
    cells = by_hk[HK_I1]["cells"]
    assert cells[0] is not None
    assert cells[1] is None
    assert cells[2] is not None


def test_captures_table_excludes_failed_snapshots(seeded_db: sqlite3.Connection):
    seeded_db.execute("UPDATE snapshots SET status = 'failed' WHERE id = 2")
    seeded_db.commit()
    result = captures_table(seeded_db, limit=20)
    snap_ids = [s["id"] for s in result["snapshots"]]
    assert 2 not in snap_ids
    assert snap_ids == [1, 3]


def test_snapshot_history_all_three_hotkeys_registered(seeded_db: sqlite3.Connection):
    """All snapshots in seed have HK_F1+HK_F2+HK_I1 with is_registered=1, status='ok'."""
    rows = snapshot_history(seeded_db, limit=10)
    assert len(rows) == 3
    # newest first
    assert [r["id"] for r in rows] == [3, 2, 1]
    for r in rows:
        assert r["status"] == "ok"
        assert r["team_size"] == 3       # HK_F1, HK_F2, HK_I1
        assert r["total_rows"] == 3
        assert r["registered"] == 3
        assert r["deregistered"] == 0
        assert r["failed"] == 0


def test_snapshot_history_counts_deregistered(seeded_db: sqlite3.Connection):
    # Mark HK_F2 as deregistered in snapshot 3
    seeded_db.execute(
        "UPDATE neuron_snapshots SET is_registered = 0 "
        "WHERE hotkey_ss58 = ? AND snapshot_id = 3",
        (HK_F2,),
    )
    seeded_db.commit()
    rows = snapshot_history(seeded_db, limit=10)
    snap3 = next(r for r in rows if r["id"] == 3)
    assert snap3["registered"] == 2
    assert snap3["deregistered"] == 1
    assert snap3["failed"] == 0


def test_snapshot_history_counts_failed_when_rows_missing(
    seeded_db: sqlite3.Connection,
):
    # Simulate worker failing to insert HK_I1 in snapshot 2 (row missing)
    seeded_db.execute(
        "DELETE FROM neuron_snapshots WHERE hotkey_ss58 = ? AND snapshot_id = 2",
        (HK_I1,),
    )
    seeded_db.execute(
        "UPDATE snapshots SET status = 'partial' WHERE id = 2"
    )
    seeded_db.commit()
    rows = snapshot_history(seeded_db, limit=10)
    snap2 = next(r for r in rows if r["id"] == 2)
    assert snap2["status"] == "partial"
    assert snap2["total_rows"] == 2  # only HK_F1 + HK_F2 left
    assert snap2["failed"] == 1


def test_snapshot_history_respects_limit(seeded_db: sqlite3.Connection):
    rows = snapshot_history(seeded_db, limit=2)
    assert len(rows) == 2
    assert [r["id"] for r in rows] == [3, 2]


def test_snapshot_history_empty_when_no_snapshots(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="X", hotkeys=[HK_F1])],
        subnet_id=56,
    )
    assert snapshot_history(memory_db) == []


def test_captures_table_empty_when_no_snapshots(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="A", hotkeys=[HK_F1])],
        subnet_id=56,
    )
    result = captures_table(memory_db, limit=20)
    assert result["snapshots"] == []
    assert result["rows"] == []


def test_dashboard_hotkey_summary_no_snapshots_yet(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(
        memory_db,
        [PersonConfig(name="A", hotkeys=[HK_F1, HK_F2])],
        subnet_id=56,
    )
    rows = dashboard_hotkey_summary(
        memory_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    assert len(rows) == 2
    for r in rows:
        assert r["cumulative"] == 0
        assert r["is_registered"] == 0
        assert r["last_refresh"] is None


# ---- Settlement queries ----

def test_last_settlement_returns_none_when_empty(seeded_db: sqlite3.Connection):
    assert last_settlement(seeded_db) is None
    assert last_settlement_snapshot_id(seeded_db) == 0


def test_create_settlement_freezes_per_hotkey_cumulative(seeded_db: sqlite3.Connection):
    # Seed cumulative: HK_F1=6.0 (1+2+3), HK_F2=1.5 (0.5*3), HK_I1=0.6 (0.1+0.2+0.3)
    settle = create_settlement(seeded_db, note="May payout")
    assert settle["note"] == "May payout"
    assert settle["settled_through_snapshot_id"] == 3
    # Total = sum of int(cumulative) per hotkey (truncated individually).
    # Real RAO values are already integers; the seed uses 1.0/0.5/0.6 floats
    # so each truncates: int(6.0)+int(1.5)+int(0.6) = 6+1+0 = 7.
    assert settle["total_cumulative_rao"] == 7
    # Lines: one per hotkey, sorted by cumulative desc
    lines = settle["lines"]
    assert len(lines) == 3
    by_hk = {line["hotkey_ss58"]: line for line in lines}
    assert by_hk[HK_F1]["cumulative_rao"] == 6
    assert by_hk[HK_F2]["cumulative_rao"] == 1
    assert by_hk[HK_I1]["cumulative_rao"] == 0  # int(0.6) = 0
    assert by_hk[HK_F1]["person_name"] == "Alice"
    assert by_hk[HK_I1]["person_name"] == "Bob"


def test_create_settlement_raises_when_no_new_snapshots(seeded_db: sqlite3.Connection):
    create_settlement(seeded_db)  # first settle covers all 3 snapshots
    with pytest.raises(ValueError, match="No new completed snapshots"):
        create_settlement(seeded_db)  # nothing new to settle


def test_dashboard_hotkey_summary_resets_after_settlement(seeded_db: sqlite3.Connection):
    create_settlement(seeded_db)
    rows = dashboard_hotkey_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    # All cumulative reset to 0 (no new snapshots since settle)
    for r in rows:
        assert r["cumulative"] == 0


def test_captures_table_resets_after_settlement(seeded_db: sqlite3.Connection):
    create_settlement(seeded_db)
    result = captures_table(seeded_db, limit=20)
    # No snapshots since settle → empty
    assert result["snapshots"] == []


def test_snapshot_history_filters_to_current_period(seeded_db: sqlite3.Connection):
    create_settlement(seeded_db)  # boundary = snapshot id 3
    rows = snapshot_history(seeded_db, limit=20)
    # Snapshots 1,2,3 are pre-boundary → excluded
    assert rows == []


def test_delete_settlement_revives_period(seeded_db: sqlite3.Connection):
    settle = create_settlement(seeded_db)
    assert delete_settlement(seeded_db, settle["id"]) is True

    # After delete, dashboard sees old data again
    rows = dashboard_hotkey_summary(
        seeded_db,
        from_dt=datetime(2000, 1, 1, tzinfo=timezone.utc),
        to_dt=datetime(2100, 1, 1, tzinfo=timezone.utc),
    )
    by_hk = {r["hotkey"]: r for r in rows}
    assert by_hk[HK_F1]["cumulative"] == pytest.approx(6.0)

    # Settlement gone
    assert last_settlement(seeded_db) is None
    # Lines cascaded
    n_lines = seeded_db.execute(
        "SELECT COUNT(*) FROM settlement_lines"
    ).fetchone()[0]
    assert n_lines == 0


def test_delete_settlement_returns_false_for_unknown(seeded_db: sqlite3.Connection):
    assert delete_settlement(seeded_db, 9999) is False


def test_list_settlements_newest_first(seeded_db: sqlite3.Connection):
    s1 = create_settlement(seeded_db, note="first")
    # Add a 4th snapshot so we can settle again
    seeded_db.execute(
        "INSERT INTO snapshots (id, taken_at, status) VALUES (4, ?, 'ok')",
        (datetime(2026, 5, 17, 16, 0, tzinfo=timezone.utc),),
    )
    seeded_db.execute(
        "INSERT INTO neuron_snapshots VALUES (4, ?, 10, 5.0, 1)", (HK_F1,)
    )
    seeded_db.commit()
    s2 = create_settlement(seeded_db, note="second")

    rows = list_settlements(seeded_db)
    assert [r["note"] for r in rows] == ["second", "first"]
    assert rows[0]["id"] == s2["id"]


def test_settlement_detail_returns_lines(seeded_db: sqlite3.Connection):
    settle = create_settlement(seeded_db, note="payout")
    detail = settlement_detail(seeded_db, settle["id"])
    assert detail is not None
    assert detail["note"] == "payout"
    # Lines ordered by cumulative_rao DESC
    assert detail["lines"][0]["cumulative_rao"] >= detail["lines"][-1]["cumulative_rao"]


def test_settlement_detail_returns_none_for_unknown(seeded_db: sqlite3.Connection):
    assert settlement_detail(seeded_db, 9999) is None


# ---- set_settlement_distribution (token-price scheme) ----

def test_set_distribution_token_price_split_30_70(seeded_db: sqlite3.Connection):
    """Compute distribution: 30% personal reward + 70% kas contribution."""
    settle = create_settlement(seeded_db, note="just close")
    assert settle["token_price_usd"] is None

    # Seed cumulative: HK_F1=6.0 RAO, HK_F2=1.5 RAO, HK_I1=0.6 RAO (total 8.1 RAO)
    updated = set_settlement_distribution(
        seeded_db, settle["id"], token_price_usd=1_000_000_000
    )
    assert updated["token_price_usd"] == 1_000_000_000

    # For HK_F1 with cum=6.0 RAO and price=1e9 USD/alpha:
    #   emission_usd = 6.0 × 1e9 / 1e9 = 6.0
    #   reward_usd   = round(6.0 × 0.3, 2) = 1.8
    #   kas_contrib  = round(6.0 - 1.8, 2) = 4.2
    line_hk_f1 = next(line for line in updated["lines"] if line["hotkey_ss58"] == HK_F1)
    assert line_hk_f1["reward_usd"] == pytest.approx(1.8)
    assert line_hk_f1["kas_contribution_usd"] == pytest.approx(4.2)
    # Personal reward + kas contribution = emission_usd exactly
    for line in updated["lines"]:
        emission_usd = line["cumulative_rao"] * 1_000_000_000 / 1_000_000_000
        assert line["reward_usd"] + line["kas_contribution_usd"] == pytest.approx(emission_usd)


def test_set_distribution_realistic_price(seeded_db: sqlite3.Connection):
    """Token price Rp 5jt per alpha; settlement total emission 8.1 RAO."""
    # Seed lines have cumulative_rao stored as REAL (e.g., 6.0) — in production
    # these would be large integers in RAO. Math should still work.
    settle = create_settlement(seeded_db)
    updated = set_settlement_distribution(
        seeded_db, settle["id"], token_price_usd=5_000_000  # Rp 5jt / α
    )
    # Total emission ≈ 8.1 RAO → emission_usd_total = round(8.1 × 5jt / 1e9)
    # That's effectively 0 because 8.1 RAO is sub-alpha. Real prod values are bigger.
    # We just check shape: totals consistent
    assert updated["total_personal_reward_usd"] >= 0
    assert updated["total_kas_contribution_usd"] >= 0


def test_set_distribution_recomputes_on_repeat(seeded_db: sqlite3.Connection):
    settle = create_settlement(seeded_db)
    a = set_settlement_distribution(seeded_db, settle["id"], token_price_usd=1_000_000_000)
    b = set_settlement_distribution(seeded_db, settle["id"], token_price_usd=2_000_000_000)
    # Doubling price should double both reward and kas contribution (roughly)
    assert b["token_price_usd"] == 2_000_000_000
    assert b["total_personal_reward_usd"] >= a["total_personal_reward_usd"]


def test_set_distribution_returns_none_for_unknown(seeded_db: sqlite3.Connection):
    assert (
        set_settlement_distribution(seeded_db, 9999, token_price_usd=1_000) is None
    )


def test_set_distribution_rejects_negative(seeded_db: sqlite3.Connection):
    settle = create_settlement(seeded_db)
    with pytest.raises(ValueError, match="non-negative"):
        set_settlement_distribution(seeded_db, settle["id"], token_price_usd=-1)


# ---- Kas Bersama ----

def test_kas_totals_empty(memory_db: sqlite3.Connection):
    init_schema(memory_db)
    sync_team(memory_db, [PersonConfig(name="X", hotkeys=[HK_F1])], subnet_id=56)
    totals = kas_totals(memory_db)
    assert totals == {"contributed": 0, "distributed": 0, "balance": 0}


def test_kas_balance_after_settlement_with_distribution(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import kas_totals
    settle = create_settlement(seeded_db)
    set_settlement_distribution(seeded_db, settle["id"], token_price_usd=1_000_000_000)
    totals = kas_totals(seeded_db)
    # contributed > 0 (the 70% from each line)
    assert totals["contributed"] > 0
    assert totals["distributed"] == 0
    assert totals["balance"] == totals["contributed"]


def test_all_time_contributions(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import all_time_contributions
    settle = create_settlement(seeded_db)
    contribs = all_time_contributions(seeded_db)
    names = {c["name"] for c in contribs}
    assert names == {"Alice", "Bob"}
    # Alice has 2 hotkeys (HK_F1, HK_F2) totaling 7.5 RAO; Bob has 1 (HK_I1) = 0.6
    alice = next(c for c in contribs if c["name"] == "Alice")
    assert alice["cumulative_rao"] == 7  # int truncation of 7.5
    bob = next(c for c in contribs if c["name"] == "Bob")
    assert bob["cumulative_rao"] == 0  # int truncation of 0.6


def test_preview_kas_distribution_sums_to_amount(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import preview_kas_distribution
    settle = create_settlement(seeded_db)
    shares = preview_kas_distribution(seeded_db, amount_usd=10_000_000)
    # Sum must equal amount exactly (last-person remainder fix)
    assert sum(s["share_usd"] for s in shares) == 10_000_000
    # Alice (more emission) > Bob
    by_name = {s["name"]: s for s in shares}
    assert by_name["Alice"]["share_usd"] >= by_name["Bob"]["share_usd"]


def test_create_and_delete_kas_distribution(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import (
        create_kas_distribution,
        delete_kas_distribution,
        kas_totals,
        list_kas_distributions,
    )
    settle = create_settlement(seeded_db)
    set_settlement_distribution(seeded_db, settle["id"], token_price_usd=1_000_000_000)
    before = kas_totals(seeded_db)
    assert before["balance"] > 0

    amount = round(before["balance"] / 2, 2)
    if amount == 0:
        amount = 1.0
    d = create_kas_distribution(seeded_db, amount_usd=amount, note="test")
    assert d["amount_usd"] == pytest.approx(amount)
    assert sum(line["share_usd"] for line in d["lines"]) == pytest.approx(amount)

    mid = kas_totals(seeded_db)
    assert mid["distributed"] == pytest.approx(amount)
    assert mid["balance"] == pytest.approx(before["balance"] - amount)

    # Delete reopens balance
    assert delete_kas_distribution(seeded_db, d["id"]) is True
    after = kas_totals(seeded_db)
    assert after["distributed"] == 0
    assert after["balance"] == pytest.approx(before["balance"])


def test_kas_distribution_rejects_overdraw(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import create_kas_distribution, kas_totals
    settle = create_settlement(seeded_db)
    set_settlement_distribution(seeded_db, settle["id"], token_price_usd=1_000_000_000)
    balance = kas_totals(seeded_db)["balance"]
    with pytest.raises(ValueError, match="Insufficient kas balance"):
        create_kas_distribution(seeded_db, amount_usd=balance + 1)


def test_kas_distribution_rejects_negative(seeded_db: sqlite3.Connection):
    from emission_tracker.web.queries import create_kas_distribution
    with pytest.raises(ValueError, match="non-negative"):
        create_kas_distribution(seeded_db, amount_usd=-1)
