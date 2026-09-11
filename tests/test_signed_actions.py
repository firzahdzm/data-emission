import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.signer.protocol import OP_PAY, SignResult
from emission_tracker.web import queries
from emission_tracker.web.errors import install_error_handlers
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
    install_error_handlers(a)
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


# Obvious dummy. The wallet unlock value is typed per action now, so every
# request that reaches the signer carries one.
UNLOCK = "dummy-unlock-value"


def _post(app, path, json=None, user="alice", secret=UNLOCK):
    body = dict(json or {})
    if secret is not None:
        body["secret"] = secret
    return TestClient(app).post(path, json=body, headers={"X-Remote-User": user})


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


def test_unconfigured_signer_leaves_no_pending_row(app, monkeypatch):
    """A missing signer must not strand a pending row: the coldkey would be
    blocked forever."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    # app.state.signer is never set in this test.
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 503
    assert queries.pending_action(app.state.db_conn, CK) is None


def test_unconfigured_fee_type_is_rejected_before_signing(app, monkeypatch):
    """A type the deployment has no price for is a 400, not a KeyError."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    fake = _FakeSigner()
    app.state.signer = fake
    app.state.config.tournament.fees_tao = {"text": 0.7, "env": 0.6}
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["image"]})
    assert r.status_code == 400
    assert fake.sent == []


def test_the_row_records_the_signers_amount_not_the_trackers_estimate(
    app, monkeypatch
):
    """The signer owns the authoritative fee table and the tracker's copy is
    explicitly untrusted, so the two can diverge. When they do, this row is
    the only durable record of what was actually paid."""
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    app.state.signer = _FakeSigner(
        result=SignResult(True, OP_PAY, CK, amount_rao=850_000_000, tx_hash="0xbeef")
    )
    r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
    assert r.status_code == 200
    row = queries.recent_actions(app.state.db_conn)[0]
    # The tracker estimated 0.7 τ from its own table; the signer moved 0.85.
    assert row["amount_rao"] == 850_000_000
    assert row["status"] == "ok"


class TestUnlockValueHandling:
    """The wallet unlock value is typed by an admin and forwarded for one
    btcli call. It must not survive anywhere after that."""

    def test_blank_value_is_refused_before_any_row_or_socket_call(
        self, app, monkeypatch
    ):
        """An empty string reaches btcli as an unset variable, which makes it
        prompt and --no-prompt turn that into an opaque exit. Fail clearly."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        fake = _FakeSigner()
        app.state.signer = fake

        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]}, secret="   ")
        assert r.status_code == 400
        assert fake.sent == []
        assert queries.recent_actions(app.state.db_conn) == []

    def test_unstake_also_requires_it(self, app, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        fake = _FakeSigner()
        app.state.signer = fake
        r = _post(app, f"/api/stake/unstake-all/{CK}", secret="")
        assert r.status_code in (400, 422)
        assert fake.sent == []

    def test_it_never_lands_in_the_audit_row(self, app, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        app.state.signer = _FakeSigner()
        _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})

        row = queries.recent_actions(app.state.db_conn)[0]
        assert UNLOCK not in " ".join(str(v) for v in row.values())

    def test_it_never_reaches_the_logs_even_when_the_action_fails(
        self, app, monkeypatch, caplog
    ):
        """A failure path is where secrets usually escape — into the error
        text, the row, or a stack trace."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        app.state.signer = _FakeSigner(
            result=SignResult(False, OP_PAY, CK, error="insufficient balance")
        )
        with caplog.at_level("DEBUG"):
            r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})

        assert r.status_code == 502
        assert UNLOCK not in caplog.text
        assert UNLOCK not in r.text
        row = queries.recent_actions(app.state.db_conn)[0]
        assert UNLOCK not in " ".join(str(v) for v in row.values())

    def test_the_request_body_model_does_not_print_it(self):
        """Pydantic models turn up in validation errors and tracebacks."""
        from emission_tracker.web.routes_api import TournamentPayBody

        body = TournamentPayBody(types=["text"], secret=UNLOCK)
        assert UNLOCK not in repr(body)
        assert UNLOCK not in str(body)


class TestValidationErrorsDoNotEchoTheBody:
    """Pydantic builds 422s against the raw body, before the model exists,
    and puts the rejected value in error["input"] — so SecretStr never gets
    a chance to mask it. 4xx bodies are exactly what proxy logs, HAR files
    and error-reporting middleware end up keeping."""

    def test_a_missing_field_does_not_echo_the_unlock_value(self, app):
        # `types` omitted: pydantic rejects the whole body, and the whole
        # body is what it would otherwise report back as "input".
        r = TestClient(app).post(
            f"/api/tournament/pay/{CK}",
            json={"secret": UNLOCK},
            headers={"X-Remote-User": "alice"},
        )
        assert r.status_code == 422
        assert UNLOCK not in r.text

    def test_a_wrong_type_does_not_echo_the_unlock_value(self, app):
        r = TestClient(app).post(
            f"/api/stake/unstake-all/{CK}",
            json={"secret": {"nested": UNLOCK}},
            headers={"X-Remote-User": "alice"},
        )
        assert r.status_code == 422
        assert UNLOCK not in r.text

    def test_the_error_still_says_what_was_wrong(self, app):
        """Stripping the input must not make the error useless."""
        r = TestClient(app).post(
            f"/api/tournament/pay/{CK}",
            json={"secret": UNLOCK},
            headers={"X-Remote-User": "alice"},
        )
        body = r.json()
        assert body["detail"][0]["loc"] == ["body", "types"]
        assert body["detail"][0]["type"] == "missing"


class TestActionStatusSurface:
    """The dashboard has to answer 'did my click work?' from the server's
    audit table, not from what this browser tab happened to do."""

    def _seed(self, conn, status, op=OP_PAY, error=None, tx=None):
        aid = queries.record_action(conn, CK, op, ["text"], 700_000_000, "alice")
        if status != "pending":
            queries.finish_action(conn, aid, status == "ok", tx, error)
        return aid

    def test_recent_actions_needs_admin(self, app, monkeypatch):
        """It names who moved money and carries btcli's error text."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        r = TestClient(app).get(
            "/api/actions/recent", headers={"X-Remote-User": "mallory"}
        )
        assert r.status_code == 403

    def test_it_reports_each_outcome(self, app, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        conn = app.state.db_conn
        self._seed(conn, "ok", tx="0xabc")

        body = TestClient(app).get(
            "/api/actions/recent", headers={"X-Remote-User": "alice"}
        ).json()
        assert body["actions"][0]["status"] == "ok"
        assert body["actions"][0]["tx_hash"] == "0xabc"
        assert body["running"] is False

    def test_running_is_true_only_while_something_is_pending(
        self, app, monkeypatch
    ):
        """The page polls on this flag, so an idle dashboard must not poll."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        conn = app.state.db_conn
        aid = self._seed(conn, "pending")

        def _running():
            return TestClient(app).get(
                "/api/actions/recent", headers={"X-Remote-User": "alice"}
            ).json()["running"]

        assert _running() is True
        queries.finish_action(conn, aid, True, "0xabc", None)
        assert _running() is False

    def test_latest_action_per_coldkey_picks_the_newest(self, app):
        conn = app.state.db_conn
        self._seed(conn, "failed", error="first attempt blew up")
        self._seed(conn, "ok", tx="0xgood")

        latest = queries.latest_action_per_coldkey(conn)
        assert latest[CK]["status"] == "ok"
        assert latest[CK]["tx_hash"] == "0xgood"

    def test_a_wallet_with_no_history_has_no_entry(self, app):
        assert queries.latest_action_per_coldkey(app.state.db_conn) == {}

    def test_the_card_shows_the_last_outcome(self, app, monkeypatch):
        """Rendered server-side, so a pending action started in another
        browser is still visible to whoever opens the page next."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        self._seed(app.state.db_conn, "pending")
        from emission_tracker.web.routes_pages import register_pages

        register_pages(app)
        html = TestClient(app).get("/", headers={"X-Remote-User": "alice"}).text
        assert "coldkey-last-pending" in html
        assert "sedang proses" in html


class TestStrandedActionsAreReaped:
    """A row goes pending before the signer is called. If the tracker dies
    or the signer is killed in between, nothing resolves it — and the
    duplicate guard then refuses every future action on that coldkey, so
    one crash silently retires a wallet."""

    def test_startup_fails_any_pending_row(self, app):
        from emission_tracker.db import cleanup_stranded_actions

        conn = app.state.db_conn
        queries.record_action(conn, CK, OP_PAY, ["text"], 700_000_000, "alice")
        assert queries.pending_action(conn, CK) is not None

        assert cleanup_stranded_actions(conn) == 1
        assert queries.pending_action(conn, CK) is None

        row = queries.recent_actions(conn)[0]
        assert row["status"] == "failed"
        # The honest record: the attempt happened, the outcome is unknown.
        assert "unknown" in row["error"]
        assert row["finished_at"] is not None

    def test_the_wallet_is_usable_again_afterwards(self, app, monkeypatch):
        from emission_tracker.db import cleanup_stranded_actions

        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        queries.record_action(app.state.db_conn, CK, OP_PAY, ["text"],
                              700_000_000, "alice")
        cleanup_stranded_actions(app.state.db_conn)

        fake = _FakeSigner()
        app.state.signer = fake
        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
        assert r.status_code == 200
        assert len(fake.sent) == 1

    def test_it_does_not_touch_settled_rows(self, app):
        from emission_tracker.db import cleanup_stranded_actions

        conn = app.state.db_conn
        done = queries.record_action(conn, CK, OP_PAY, ["text"], 1, "alice")
        queries.finish_action(conn, done, True, "0xabc", None)

        assert cleanup_stranded_actions(conn) == 0
        assert queries.recent_actions(conn)[0]["status"] == "ok"


class TestUnknownBalanceDoesNotBlockPayment:
    """A failed balance fetch writes NULL, deliberately, so the dashboard
    never shows a stale number as current. But NULL means 'unknown', not
    'zero' — conflating them let one TaoStats hiccup disable payments from
    a funded wallet until the next daily refresh."""

    def _clear_balances(self, app):
        app.state.db_conn.execute("DELETE FROM coldkey_balances")
        app.state.db_conn.commit()

    def test_a_null_reading_defers_to_btcli_instead_of_refusing(
        self, app, monkeypatch
    ):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        self._clear_balances(app)
        app.state.db_conn.execute(
            "INSERT INTO coldkey_balances (coldkey_ss58, fetched_at, "
            "balance_free_rao, tournament_seen) VALUES (?, ?, NULL, 1)",
            (CK, "2026-09-11T09:45:00+00:00"),
        )
        app.state.db_conn.commit()

        fake = _FakeSigner()
        app.state.signer = fake
        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
        assert r.status_code == 200
        assert len(fake.sent) == 1

    def test_a_real_zero_still_refuses(self, app, monkeypatch):
        """An actual reading of zero is knowledge, and it should still
        fail fast rather than spend a chain round trip."""
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        self._clear_balances(app)
        app.state.db_conn.execute(
            "INSERT INTO coldkey_balances (coldkey_ss58, fetched_at, "
            "balance_free_rao, tournament_seen) VALUES (?, ?, 0, 1)",
            (CK, "2026-09-11T09:45:00+00:00"),
        )
        app.state.db_conn.commit()

        fake = _FakeSigner()
        app.state.signer = fake
        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
        assert r.status_code == 409
        assert fake.sent == []

    def test_a_wallet_never_read_at_all_also_defers(self, app, monkeypatch):
        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        self._clear_balances(app)
        fake = _FakeSigner()
        app.state.signer = fake
        r = _post(app, f"/api/tournament/pay/{CK}", {"types": ["text"]})
        assert r.status_code == 200
