import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.routes_api import router as api_router


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"


@pytest.fixture
def app_with_db():
    # TestClient dispatches requests on a worker thread, so the connection must
    # allow cross-thread use (mirrors what the production app should do).
    conn = sqlite3.connect(
        ":memory:",
        detect_types=sqlite3.PARSE_DECLTYPES,
        check_same_thread=False,
    )
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    init_schema(conn)
    sync_team(
        conn,
        [PersonConfig(name="Alice", hotkeys=[HK_F1, HK_F2])],
        subnet_id=56,
    )
    # Use a timestamp guaranteed to be in the past so the default "all" range
    # (epoch -> now) includes the snapshot regardless of wall clock at test time.
    base = datetime(2026, 5, 17, 0, 0, tzinfo=timezone.utc)
    conn.execute(
        "INSERT INTO snapshots (id, taken_at, block_number, status) VALUES (1, ?, 1000, 'ok')",
        (base,),
    )
    conn.execute(
        "INSERT INTO neuron_snapshots VALUES (1, ?, 10, 1.0, 1)", (HK_F1,)
    )
    conn.execute(
        "INSERT INTO neuron_snapshots VALUES (1, ?, 11, 0.5, 1)", (HK_F2,)
    )
    conn.commit()

    app = FastAPI()
    app.state.db_conn = conn
    app.include_router(api_router, prefix="/api")
    yield app
    conn.close()


def test_get_persons_returns_dashboard_data(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons")
    assert resp.status_code == 200
    data = resp.json()
    assert data["persons"][0]["name"] == "Alice"
    assert data["persons"][0]["cumulative"] == pytest.approx(1.5)


def test_get_snapshots_latest(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/snapshots/latest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == 1
    assert data["status"] == "ok"


def test_get_person_series(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons/Alice/series")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["series"]) == 1
    assert data["series"][0]["cumulative"] == pytest.approx(1.5)


def test_invalid_range_returns_400(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/persons?from=2026-05-20&to=2026-05-10")
    assert resp.status_code == 400


def test_healthz(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/healthz")
    assert resp.status_code == 200
    assert resp.text.strip('"') == "ok"


def test_list_snapshots(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/snapshots")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 20
    # Fixture seeds exactly 1 snapshot (id=1, status='ok', block_number=1000)
    assert len(data["snapshots"]) == 1
    s = data["snapshots"][0]
    assert s["id"] == 1
    assert s["status"] == "ok"
    assert s["block_number"] == 1000


def test_list_snapshots_respects_limit(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/snapshots?limit=5")
    assert resp.status_code == 200
    assert resp.json()["limit"] == 5


def test_list_snapshots_rejects_invalid_limit(app_with_db):
    client = TestClient(app_with_db)
    # FastAPI returns 422 for query param validation failures
    resp = client.get("/api/snapshots?limit=0")
    assert resp.status_code == 422
    resp = client.get("/api/snapshots?limit=999")
    assert resp.status_code == 422


# ---- Settlement endpoints ----

from types import SimpleNamespace


def _set_admin_users(app, users: list[str]):
    app.state.config = SimpleNamespace(admin_users=users)


def test_post_settlement_requires_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    # No header → 401
    resp = client.post("/api/settlements", json={})
    assert resp.status_code == 401
    # Non-admin → 403
    resp = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "bob"}
    )
    assert resp.status_code == 403


def test_post_settlement_creates_record_when_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    resp = client.post(
        "/api/settlements",
        json={"note": "May payout"},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["note"] == "May payout"
    assert data["settled_through_snapshot_id"] == 1
    assert "lines" in data


def test_post_settlement_returns_400_when_nothing_to_settle(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    # First settlement succeeds
    client.post("/api/settlements", json={}, headers={"X-Remote-User": "alice"})
    # Second one has nothing new
    resp = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    assert resp.status_code == 400
    assert "No new completed snapshots" in resp.json()["detail"]


def test_delete_settlement_requires_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]

    # No auth
    resp = client.delete(f"/api/settlements/{settlement_id}")
    assert resp.status_code == 401
    # Non-admin
    resp = client.delete(
        f"/api/settlements/{settlement_id}", headers={"X-Remote-User": "bob"}
    )
    assert resp.status_code == 403


def test_delete_settlement_admin_204(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]

    resp = client.delete(
        f"/api/settlements/{settlement_id}", headers={"X-Remote-User": "alice"}
    )
    assert resp.status_code == 204

    # Settlement gone
    resp = client.get(f"/api/settlements/{settlement_id}")
    assert resp.status_code == 404


def test_delete_settlement_404_unknown(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    resp = client.delete(
        "/api/settlements/9999", headers={"X-Remote-User": "alice"}
    )
    assert resp.status_code == 404


def test_get_settlements_list_no_auth_required(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    client.post("/api/settlements", json={"note": "first"}, headers={"X-Remote-User": "alice"})
    # GET without auth header still works (read-only is public to logged-in basic-auth users)
    resp = client.get("/api/settlements")
    assert resp.status_code == 200
    data = resp.json()
    assert data["limit"] == 50
    assert len(data["settlements"]) == 1
    assert data["settlements"][0]["note"] == "first"


def test_get_settlement_detail_includes_lines(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]

    resp = client.get(f"/api/settlements/{settlement_id}")
    assert resp.status_code == 200
    detail = resp.json()
    assert detail["id"] == settlement_id
    assert "lines" in detail
    assert len(detail["lines"]) == 2  # HK_F1 + HK_F2 from app_with_db fixture


def test_put_distribution_requires_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]

    body = {"token_price_idr": 5_000_000}
    # no auth → 401
    resp = client.put(f"/api/settlements/{settlement_id}/distribution", json=body)
    assert resp.status_code == 401
    # non-admin → 403
    resp = client.put(
        f"/api/settlements/{settlement_id}/distribution",
        json=body,
        headers={"X-Remote-User": "bob"},
    )
    assert resp.status_code == 403


def test_put_distribution_computes_then_edits(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]
    # Initially no distribution
    assert create.json()["token_price_idr"] is None

    # Compute with token_price=1e9 (1 IDR per RAO)
    resp = client.put(
        f"/api/settlements/{settlement_id}/distribution",
        json={"token_price_idr": 1_000_000_000},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_price_idr"] == 1_000_000_000
    first_reward = data["total_personal_reward_idr"]
    first_kas = data["total_kas_contribution_idr"]
    # 70% kas vs 30% reward → kas ≈ 2.33 × reward (approx, rounding)
    assert first_kas > first_reward

    # Edit: double the price → both numbers should double (approximately)
    resp = client.put(
        f"/api/settlements/{settlement_id}/distribution",
        json={"token_price_idr": 2_000_000_000},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["token_price_idr"] == 2_000_000_000
    assert data["total_personal_reward_idr"] >= first_reward


def test_put_distribution_404_for_unknown(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    resp = client.put(
        "/api/settlements/9999/distribution",
        json={"token_price_idr": 100},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 404


def test_put_distribution_400_for_negative(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    create = client.post(
        "/api/settlements", json={}, headers={"X-Remote-User": "alice"}
    )
    settlement_id = create.json()["id"]
    resp = client.put(
        f"/api/settlements/{settlement_id}/distribution",
        json={"token_price_idr": -100},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 400


# ---- Kas API ----


def _settle_and_set_price(client, settlement_note="period"):
    client.post(
        "/api/settlements",
        json={"note": settlement_note},
        headers={"X-Remote-User": "alice"},
    )
    # Settlement #1 created; set distribution to populate kas
    client.put(
        "/api/settlements/1/distribution",
        json={"token_price_idr": 1_000_000_000},
        headers={"X-Remote-User": "alice"},
    )


def test_get_kas_balance(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    resp = client.get("/api/kas/balance")
    assert resp.status_code == 200
    data = resp.json()
    assert "contributed" in data and "distributed" in data and "balance" in data

    _settle_and_set_price(client)
    resp = client.get("/api/kas/balance")
    data = resp.json()
    assert data["balance"] > 0


def test_kas_preview_sums_to_amount(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    _settle_and_set_price(client)
    resp = client.get("/api/kas/preview?amount_idr=1000000")
    assert resp.status_code == 200
    shares = resp.json()["shares"]
    assert sum(s["share_idr"] for s in shares) == 1_000_000


def test_post_kas_distribution_requires_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    _settle_and_set_price(client)
    # no auth → 401
    resp = client.post("/api/kas/distributions", json={"amount_idr": 100})
    assert resp.status_code == 401
    # non-admin → 403
    resp = client.post(
        "/api/kas/distributions",
        json={"amount_idr": 100},
        headers={"X-Remote-User": "bob"},
    )
    assert resp.status_code == 403


def test_post_kas_distribution_admin_201(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    _settle_and_set_price(client)
    balance = client.get("/api/kas/balance").json()["balance"]
    resp = client.post(
        "/api/kas/distributions",
        json={"amount_idr": balance, "note": "first payout"},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["amount_idr"] == balance
    assert sum(line["share_idr"] for line in data["lines"]) == balance


def test_post_kas_distribution_rejects_overdraw(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    _settle_and_set_price(client)
    balance = client.get("/api/kas/balance").json()["balance"]
    resp = client.post(
        "/api/kas/distributions",
        json={"amount_idr": balance + 999_999_999},
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 400
    assert "Insufficient" in resp.json()["detail"]


def test_delete_kas_distribution_admin(app_with_db):
    _set_admin_users(app_with_db, ["alice"])
    client = TestClient(app_with_db)
    _settle_and_set_price(client)
    balance_before = client.get("/api/kas/balance").json()["balance"]
    create = client.post(
        "/api/kas/distributions",
        json={"amount_idr": balance_before},
        headers={"X-Remote-User": "alice"},
    )
    dist_id = create.json()["id"]
    assert client.get("/api/kas/balance").json()["balance"] == 0
    resp = client.delete(
        f"/api/kas/distributions/{dist_id}",
        headers={"X-Remote-User": "alice"},
    )
    assert resp.status_code == 204
    # Balance back to original
    assert client.get("/api/kas/balance").json()["balance"] == balance_before


def test_kas_distribution_detail_404(app_with_db):
    client = TestClient(app_with_db)
    resp = client.get("/api/kas/distributions/9999")
    assert resp.status_code == 404
