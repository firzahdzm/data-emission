import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.signer.protocol import OP_PAY, SignResult
from emission_tracker.web import queries
from emission_tracker.web.routes_api import router as api_router

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"
HK = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"


class _FakeSigner:
    def __init__(self, result=None, boom=None):
        self.sent = []
        self._result = result
        self._boom = boom

    def send(self, request):
        self.sent.append(request)
        if self._boom:
            raise self._boom
        return self._result or SignResult(
            True, request.op, request.coldkey,
            amount_rao=700_000_000, tx_hash="0xdead",
        )


@pytest.fixture
def app():
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    sync_team(conn, [PersonConfig(name="Alice",
                                  hotkeys=[{"hotkey": HK, "coldkey": CK}])],
              subnet_id=56)
    # Enough free balance for text+env (1.3 tau) but short of
    # text+image+env (1.7 tau).
    conn.execute(
        "INSERT INTO coldkey_balances (coldkey_ss58, fetched_at, balance_free_rao, "
        "tournament_seen) VALUES (?, '2026-09-11T00:00:00+00:00', 1500000000, 1)",
        (CK,),
    )
    conn.commit()
    a = FastAPI()
    a.include_router(api_router, prefix="/api")
    a.state.db_conn = conn
    a.state.config = SimpleNamespace(
        admin_users=["alice"],
        proxy_secret="",
        tournament=SimpleNamespace(
            address="5Ef5", fees_tao={"text": 0.7, "image": 0.4, "env": 0.6}
        ),
    )
    yield a
    conn.close()


def _post(app, path, json=None, user="alice"):
    return TestClient(app).post(path, json=json, headers={"X-Remote-User": user})


def test_admin_pays_and_the_attempt_is_recorded(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake

    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 200
    assert fake.sent[0].op == OP_PAY
    assert fake.sent[0].types == ("text",)

    row = queries.recent_actions(app.state.db_conn)[0]
    assert row["status"] == "ok"
    assert row["tx_hash"] == "0xdead"
    assert row["requested_by"] == "alice"


def test_the_request_never_carries_an_amount(app, monkeypatch):
    """The signer decides amounts; if the tracker could name one, moving
    the boundary to the signer would have bought nothing."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    _post(app, f"/api/tournament/pay/{CK}", {"types": ["text", "env"]})
    assert not hasattr(fake.sent[0], "amount_tao")
    assert not hasattr(fake.sent[0], "amount_rao")


def test_non_admin_cannot_pay(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]}, user="mallory")
    assert r.status_code == 403
    assert fake.sent == []


def test_insufficient_balance_is_refused_before_signing(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    # 1.7 τ needed, 0.9 τ available.
    r = _post(app, f"/api/tournament/pay/{CK}",
              {"types": ["text", "image", "env"]})
    assert r.status_code == 409
    assert fake.sent == []


def test_a_pending_action_blocks_a_second_one(app, monkeypatch):
    """A double click or a reload must not pay twice."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    queries.record_action(app.state.db_conn, CK, OP_PAY, ["text"],
                          700_000_000, "alice")
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 409
    assert fake.sent == []


def test_unknown_coldkey_is_refused(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, "/api/tournament/pay/5NOPE", {"types": ["text"]})
    assert r.status_code == 404
    assert fake.sent == []


def test_signer_failure_marks_the_row_failed_and_reports_it(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    app.state.signer = _FakeSigner(
        result=SignResult(False, OP_PAY, CK, error="insufficient balance")
    )
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 502
    row = queries.recent_actions(app.state.db_conn)[0]
    assert row["status"] == "failed"
    assert "insufficient balance" in row["error"]
    # Crucially not left pending, or the coldkey is blocked forever.
    assert queries.pending_action(app.state.db_conn, CK) is None


def test_unstake_endpoint_sends_no_types(app, monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    r = _post(app, f"/api/stake/unstake-all/{CK}")
    assert r.status_code == 200
    assert fake.sent[0].types == ()
