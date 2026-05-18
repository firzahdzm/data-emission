import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.routes_pages import register_pages


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


@pytest.fixture
def app(memory_db: sqlite3.Connection):
    # NOTE: FastAPI TestClient runs on a worker thread → need check_same_thread=False.
    # Use a fresh connection (not the conftest fixture) to allow cross-thread access.
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    sync_team(
        conn,
        [PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2])],
        subnet_id=56,
    )
    # Use midnight UTC to avoid future-timestamp issues vs sandbox clock
    conn.execute(
        "INSERT INTO snapshots (id, taken_at, status) VALUES (1, ?, 'ok')",
        (datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc),),
    )
    # Emission stored in RAO (10^9 = 1 alpha). 1.0 α + 0.5 α = 1.5 α total.
    conn.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 10, 1_000_000_000, 1)", (HK_F1,))
    conn.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 11, 500_000_000, 1)", (HK_F2,))
    conn.commit()
    a = FastAPI()
    a.state.db_conn = conn
    register_pages(a)
    yield a
    conn.close()


def test_dashboard_renders_with_person_row(app):
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "Alice" in resp.text
    # 1.5 alpha (= 1.5 × 10^9 RAO seeded) should be displayed as "1.5000 α"
    assert "1.5000 α" in resp.text


def test_dashboard_per_hotkey_rows_and_status(app):
    """New dashboard renders one row per hotkey with truncated address + status."""
    client = TestClient(app)
    resp = client.get("/")
    # Per-hotkey amounts
    assert "1.0000 α" in resp.text  # HK_F1 row
    assert "0.5000 α" in resp.text  # HK_F2 row
    # Both rows currently registered
    assert "registered" in resp.text
    # Hotkey address shown truncated (first 8 chars must appear)
    assert HK_F1[:8] in resp.text
    assert HK_F2[:8] in resp.text


def test_dashboard_accepts_date_inputs(app):
    """from/to query params should be parsed and applied as the range."""
    client = TestClient(app)
    # 2026-05-17 ≤ taken_at < 2026-05-18 covers the seeded snapshot
    resp = client.get("/?from=2026-05-17&to=2026-05-18")
    assert resp.status_code == 200
    assert "1.5000 α" in resp.text
    # Period header should reflect the requested dates (in WIB display)
    assert "2026-05-17" in resp.text
    # Date inputs in the form should be pre-filled with the requested values
    assert 'value="2026-05-17"' in resp.text
    assert 'value="2026-05-18"' in resp.text


def test_dashboard_empty_range_yields_zero(app):
    """A date range with no snapshots should render 0 α without crashing."""
    client = TestClient(app)
    resp = client.get("/?from=2020-01-01&to=2020-01-02")
    assert resp.status_code == 200
    assert "0.0000 α" in resp.text


def test_dashboard_with_range_preset(app):
    client = TestClient(app)
    resp = client.get("/?range=7d")
    # Range "7d" may exclude the 2026-05-17 snapshot depending on wall clock;
    # we just verify the page renders with the right shape.
    assert resp.status_code == 200
    assert "Alice" in resp.text


def test_person_page_renders(app):
    client = TestClient(app)
    resp = client.get("/person/Alice")
    assert resp.status_code == 200
    assert "Alice" in resp.text
    assert HK_F1 in resp.text


def test_dashboard_period_trimmed_to_seconds_in_wib(app):
    """Period range timestamps must be displayed in WIB (UTC+7), no microseconds."""
    client = TestClient(app)
    resp = client.get("/")
    assert "Period: " in resp.text
    period_line = next(
        line for line in resp.text.splitlines() if "Period:" in line
    )
    # Must be tagged WIB
    assert "WIB" in period_line
    # No microsecond noise
    assert ".000000" not in period_line
    assert ".999999" not in period_line
    # Epoch start in WIB = 1970-01-01 07:00:00 WIB
    assert "1970-01-01 07:00:00 WIB" in period_line


def test_format_dt_seconds_helper():
    from datetime import datetime, timezone

    from emission_tracker.web.routes_pages import _format_dt_seconds

    # UTC 14:23:45 → WIB 21:23:45
    dt = datetime(2026, 5, 17, 14, 23, 45, 567890, tzinfo=timezone.utc)
    assert _format_dt_seconds(dt) == "2026-05-17 21:23:45 WIB"

    # SQLite-style ISO string with microseconds + tz → convert + trim
    assert (
        _format_dt_seconds("2026-05-17 18:43:02.250567+00:00")
        == "2026-05-18 01:43:02 WIB"
    )
    # ISO without microseconds → convert
    assert _format_dt_seconds("2026-05-17 18:43:02+00:00") == "2026-05-18 01:43:02 WIB"
    # Naive datetime → assumed UTC
    naive = datetime(2026, 5, 17, 18, 0, 0)
    assert _format_dt_seconds(naive) == "2026-05-18 01:00:00 WIB"
    # None / empty → empty string
    assert _format_dt_seconds(None) == ""
    assert _format_dt_seconds("") == ""
