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
    conn.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 10, 1.0, 1)", (HK_F1,))
    conn.execute("INSERT INTO neuron_snapshots VALUES (1, ?, 11, 0.5, 1)", (HK_F2,))
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
    assert "1.5" in resp.text  # cumulative


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
