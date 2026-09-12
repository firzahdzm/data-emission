import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from emission_tracker.config import PersonConfig
from emission_tracker.db import init_schema, sync_team
from emission_tracker.web.routes_pages import register_pages


HK_F1 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA1"
HK_F2 = "5AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA2"
CK_F1 = "5CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC1"


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
        [
            PersonConfig(
                name="Alice",
                hotkeys=[
                    {"hotkey": HK_F1, "coldkey": CK_F1},
                    {"hotkey": HK_F2, "coldkey": CK_F1},
                ],
            )
        ],
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


def test_captures_page_renders(app):
    client = TestClient(app)
    resp = client.get("/captures")
    assert resp.status_code == 200
    # Page header (CSS upper-cases this, but raw HTML keeps casing as-is)
    assert "Emission captures" in resp.text
    # Both hotkeys appear as truncated codes
    assert HK_F1[:8] in resp.text
    assert HK_F2[:8] in resp.text
    # Per-snapshot emission cells appear (1.0 α and 0.5 α from seed)
    assert "1.0000 α" in resp.text
    assert "0.5000 α" in resp.text


def test_captures_page_limit_param(app):
    client = TestClient(app)
    resp = client.get("/captures?limit=5")
    assert resp.status_code == 200
    # The numeric input echoes the limit
    assert 'value="5"' in resp.text


def test_archive_page_empty(app):
    client = TestClient(app)
    resp = client.get("/archive")
    assert resp.status_code == 200
    assert "No settlements yet" in resp.text


def test_archive_page_shows_settlement(app):
    from emission_tracker.web.queries import create_settlement
    create_settlement(app.state.db_conn, token_price_usd=1.0, note="Week 1")
    client = TestClient(app)
    resp = client.get("/archive")
    assert resp.status_code == 200
    assert "Week 1" in resp.text


def test_archive_detail_404_for_unknown(app):
    client = TestClient(app)
    resp = client.get("/archive/9999")
    assert resp.status_code == 404


def test_archive_detail_renders_lines(app):
    from emission_tracker.web.queries import create_settlement
    settle = create_settlement(app.state.db_conn, token_price_usd=1.0, note="Test")
    client = TestClient(app)
    resp = client.get(f"/archive/{settle['id']}")
    assert resp.status_code == 200
    # Hotkey codes appear (truncated)
    assert HK_F1[:8] in resp.text
    assert HK_F2[:8] in resp.text
    # Note shown
    assert "Test" in resp.text


def test_dashboard_close_button_hidden_for_non_admin(app):
    # No admin_users configured on this app → never admin
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert "close-period-btn" not in resp.text


def test_dashboard_close_button_visible_for_admin(app):
    from types import SimpleNamespace
    app.state.config = SimpleNamespace(admin_users=["alice"])
    client = TestClient(app)
    resp = client.get("/", headers={"X-Remote-User": "alice"})
    assert resp.status_code == 200
    assert "close-period-btn" in resp.text


def test_kas_page_renders_with_salary_section(app):
    client = TestClient(app)
    resp = client.get("/kas")
    assert resp.status_code == 200
    assert "Total salary paid" in resp.text
    assert "Salary history" in resp.text
    assert "No salary payments yet." in resp.text


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


class TestDashboardAdminScripts:
    """Jinja discards anything a child template puts outside its block, so a
    <script> appended past {% endblock %} vanishes silently: the button still
    renders, but nothing is ever wired to it. These tests pin the handler to
    the response body, where a missing block is visible."""

    def _html(self, app, monkeypatch, user: str | None) -> str:
        from types import SimpleNamespace

        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        app.state.config = SimpleNamespace(
            admin_users=["alice"],
            tournament=SimpleNamespace(
                address="5Ef5",
                fees_tao={"text": 0.7, "image": 0.4, "env": 0.6},
            ),
        )
        headers = {"X-Remote-User": user} if user else {}
        r = TestClient(app).get("/", headers=headers)
        assert r.status_code == 200
        return r.text

    def test_admin_gets_the_refresh_button_and_its_handler(self, app, monkeypatch):
        html = self._html(app, monkeypatch, "alice")
        assert 'id="balance-refresh-btn"' in html
        # The button is inert without these three.
        assert "/api/balances/refresh" in html
        assert "/api/balances/status" in html
        assert "addEventListener('click'" in html

    def test_every_script_block_survives_the_template_block(self, app, monkeypatch):
        """Guards the whole file, not just this one handler: no <script> may
        be stranded outside {% block content %}."""
        from pathlib import Path

        import emission_tracker.web.routes_pages as rp

        template = (
            Path(rp.__file__).parent / "templates" / "dashboard.html"
        ).read_text()
        endblock = template.index("{% endblock %}")
        assert "<script>" not in template[endblock:], (
            "a <script> sits after {% endblock %} and will never render"
        )

    def test_non_admin_gets_neither_button_nor_handler(self, app, monkeypatch):
        html = self._html(app, monkeypatch, "mallory")
        assert 'id="balance-refresh-btn"' not in html
        assert "/api/balances/refresh" not in html

    def test_admin_sees_the_tournament_controls_and_their_handler(
        self, app, monkeypatch
    ):
        html = self._html(app, monkeypatch, "alice")
        assert 'class="tournament-type"' in html
        assert "/api/tournament/pay/" in html
        assert "/api/stake/unstake-all/" in html
        # Irreversible and slippage-bearing: must not be a bare click.
        assert "confirm" in html.lower()

    def test_non_admin_sees_no_money_controls(self, app, monkeypatch):
        html = self._html(app, monkeypatch, "mallory")
        assert 'class="tournament-type"' not in html
        assert "/api/tournament/pay/" not in html
        assert "/api/stake/unstake-all/" not in html


def test_the_action_log_starts_collapsed(app, monkeypatch):
    """A log panel that renders open eats a third of the dashboard on
    every page load; it is worth a glance after a click, not standing
    room. Collapsed unless the operator opened it last time."""
    from types import SimpleNamespace

    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    app.state.config = SimpleNamespace(
        admin_users=["alice"],
        tournament=SimpleNamespace(
            address="5Ef5", fees_tao={"text": 0.7, "image": 0.4, "env": 0.6}
        ),
    )
    html = TestClient(app).get("/", headers={"X-Remote-User": "alice"}).text

    start = html.index('id="actions-panel"')
    tag = html[html.rindex("<", 0, start):html.index(">", start)]
    assert tag.startswith("<details")
    assert " open" not in tag
    assert "loadActions" in html


class TestBalancesShowTheirAge:
    """A card showing "Stake 0.71 τ" sat next to btcli reporting nothing
    to unstake. Both were right: the chain had changed and the card was
    most of a day old. Without the age, the stale figure reads as the
    chain's answer."""

    def test_the_card_says_how_old_its_figures_are(self, app, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        app.state.config = SimpleNamespace(admin_users=["alice"], subnet_id=56)
        html = TestClient(app).get("/", headers={"X-Remote-User": "alice"}).text
        assert "coldkey-age" in html

    def test_a_money_action_re_reads_the_wallet_before_reloading(
        self, app, monkeypatch
    ):
        """Reloading alone re-renders the same stored balances, so the
        operator would see pre-transaction figures after a payment."""
        from types import SimpleNamespace

        monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
        app.state.config = SimpleNamespace(
            admin_users=["alice"],
            subnet_id=56,
            tournament=SimpleNamespace(
                address="5Ef5", fees_tao={"text": 0.7}
            ),
        )
        html = TestClient(app).get("/", headers={"X-Remote-User": "alice"}).text
        assert "refreshOneColdkey(coldkey)" in html


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(seconds=30), "baru saja"),
        (timedelta(minutes=20), "20 menit lalu"),
        (timedelta(hours=14), "14 jam lalu"),
        (timedelta(days=3), "3 hari lalu"),
    ],
)
def test_age_reads_as_a_person_would_say_it(delta, expected):
    from emission_tracker.web.routes_pages import _format_age

    now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
    assert _format_age(now - delta, now=now) == expected


def test_age_of_a_wallet_never_read_says_so():
    from emission_tracker.web.routes_pages import _format_age

    assert _format_age(None) == "belum pernah"
