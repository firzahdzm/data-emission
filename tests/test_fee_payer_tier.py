"""The middle tier: may pay tournament fees, nothing else.

Paying a fee is the one spend where the signer owns both the
destination and the price, so the worst this tier can do is pay a fee
that was not due. Unstaking and treasury transfers decide an amount at
execution and sell into a market; those stay with admins. These tests
are the fence between the two.
"""

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.signer.protocol import SignResult
from emission_tracker.web.errors import install_error_handlers
from emission_tracker.web.routes_api import router as api_router
from emission_tracker.web.routes_pages import register_pages

CK = "5FnhiibtJkvCDSnfrp1iUQiZZaYJv31h114Pv7wtoztt9FP9"
HK = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
PARENT = "5HERhLCKSpmTiRD6EpnsY7DUnVqUThaANhYgXYAWqZZ28fLB"
UNLOCK = "dummy-unlock-value"


class _FakeSigner:
    def __init__(self):
        self.sent = []

    def send(self, request):
        self.sent.append(request)
        return SignResult(True, request.op, request.coldkey,
                          amount_rao=request.amount_rao or 700_000_000,
                          tx_hash="0xdead")


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    sync_team(conn, [PersonConfig(name="Alice",
                                  hotkeys=[{"hotkey": HK, "coldkey": CK}])],
              subnet_id=56)
    conn.execute(
        "INSERT INTO coldkey_balances (coldkey_ss58, fetched_at, "
        "balance_free_rao, tournament_seen) VALUES "
        "(?, '2026-09-14T00:00:00+00:00', 5000000000, 1)", (CK,),
    )
    conn.commit()

    a = FastAPI()
    install_error_handlers(a)
    a.include_router(api_router, prefix="/api")
    register_pages(a)
    a.state.db_conn = conn
    a.state.signer = _FakeSigner()
    a.state.config = SimpleNamespace(
        admin_users=["alice"],
        fee_users=["susnet"],
        proxy_secret="",
        subnet_id=56,
        treasury_coldkey=PARENT,
        tournament=SimpleNamespace(
            address="5Ef5", fees_tao={"text": 0.7, "image": 0.4, "env": 0.6}
        ),
    )
    yield a
    conn.close()


def _post(app, path, body=None, user="susnet"):
    payload = dict(body or {})
    payload.setdefault("secret", UNLOCK)
    return TestClient(app).post(path, json=payload,
                                headers={"X-Remote-User": user})


def _get(app, path, user="susnet"):
    return TestClient(app).get(path, headers={"X-Remote-User": user})


class TestWhatTheFeePayerMayDo:
    def test_they_can_pay_a_tournament_fee(self, app):
        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
        assert r.status_code == 200
        assert app.state.signer.sent[0].op == "pay_tournament"

    def test_they_can_read_the_action_history(self, app):
        """They spend; they have to be able to see whether it worked."""
        assert _get(app, "/api/actions/recent").status_code == 200

    def test_they_can_see_the_dashboard(self, app):
        assert _get(app, "/").status_code == 200


class TestWhatTheFeePayerMayNot:
    def test_they_cannot_unstake(self, app):
        r = _post(app, f"/api/stake/unstake-all/{CK}")
        assert r.status_code == 403
        assert app.state.signer.sent == []

    def test_they_cannot_sweep_to_the_treasury(self, app):
        r = _post(app, f"/api/treasury/sweep/{CK}")
        assert r.status_code == 403
        assert app.state.signer.sent == []

    def test_they_cannot_distribute_from_the_treasury(self, app):
        r = _post(app, f"/api/treasury/distribute/{CK}",
                  {"amount_rao": 1_000_000_000})
        assert r.status_code == 403
        assert app.state.signer.sent == []

    def test_they_cannot_read_every_wallet_balance_through_the_signer(self, app):
        assert _get(app, "/api/treasury/balances").status_code == 403

    def test_they_cannot_spend_the_taostats_quota(self, app):
        assert _post(app, "/api/balances/refresh").status_code == 403

    def test_they_cannot_close_a_period(self, app):
        r = TestClient(app).post(
            "/api/settlements",
            json={"token_price_usd": 1.0},
            headers={"X-Remote-User": "susnet"},
        )
        assert r.status_code == 403


class TestWhatTheFeePayerSees:
    def _html(self, app, user):
        return _get(app, "/", user=user).text

    def test_the_pay_controls_are_there(self, app):
        html = self._html(app, "susnet")
        assert 'class="tournament-type"' in html
        assert "/api/tournament/pay/" in html

    def test_the_unstake_and_treasury_buttons_are_not(self, app):
        """Hidden, not merely refused: a button that can only 403
        teaches people to ignore errors."""
        html = self._html(app, "susnet")
        assert 'class="btn-subtle unstake-all"' not in html
        assert 'id="bulk-unstake-btn"' not in html
        assert 'id="sweep-btn"' not in html
        assert 'id="distribute-btn"' not in html
        assert 'id="balance-refresh-btn"' not in html
        assert 'id="close-period-btn"' not in html

    def test_they_can_see_what_their_payment_did(self, app):
        """The card strip and the history panel are the only feedback a
        payment gives; without them the tier can spend blind."""
        html = self._html(app, "susnet")
        assert "coldkey-last-action" in html
        assert 'id="actions-panel"' in html
        assert "loadActions" in html

    def test_an_admin_still_sees_everything(self, app):
        html = self._html(app, "alice")
        for marker in ('class="btn-subtle unstake-all"', 'id="bulk-unstake-btn"',
                       'id="sweep-btn"', 'id="distribute-btn"',
                       'id="balance-refresh-btn"', 'class="tournament-type"'):
            assert marker in html

    def test_a_plain_viewer_sees_no_money_controls_at_all(self, app):
        html = self._html(app, "penonton")
        assert 'class="tournament-type"' not in html
        assert "/api/tournament/pay/" not in html
        assert 'id="actions-panel"' not in html
