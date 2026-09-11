"""The login page and the gate in front of it.

This replaced nginx Basic Auth, which means these tests now stand where
the web server used to: nothing else checks whether a stranger can reach
the dashboard, or the buttons that spend from the wallets.
"""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from emission_tracker.config import AuthConfig
from emission_tracker.web import sessions
from emission_tracker.web.passwords import hash_password, verify_password
from emission_tracker.web.routes_auth import LoginThrottle, register_auth

PASSWORD = "sandi-yang-panjang"
SECRET = "session-signing-secret"


@pytest.fixture
def app(monkeypatch):
    monkeypatch.delenv("EMISSION_DEV_USER", raising=False)
    app = FastAPI()
    register_auth(app)

    @app.get("/")
    def home():
        return JSONResponse({"page": "dashboard"})

    @app.post("/api/tournament/pay/{coldkey}")
    def pay(coldkey: str):
        return JSONResponse({"paid": coldkey})

    app.state.config = SimpleNamespace(
        admin_users=["admin"],
        proxy_secret="",
        auth=AuthConfig(
            users={"admin": hash_password(PASSWORD)},
            session_secret=SECRET,
            session_hours=1,
        ),
    )
    return app


@pytest.fixture
def client(app):
    # https, because the session cookie is Secure on a real deployment
    # and a plain-http client would silently drop it.
    return TestClient(app, base_url="https://testserver", follow_redirects=False)


def _login(client, username="admin", password=PASSWORD, **kw):
    return client.post(
        "/login",
        data={"username": username, "password": password, **kw},
    )


class TestTheGate:
    def test_a_stranger_is_sent_to_the_login_page(self, client):
        r = client.get("/")
        assert r.status_code == 303
        assert r.headers["location"] == "/login?next=/"

    def test_a_stranger_calling_the_money_api_gets_401_not_a_redirect(self, client):
        """A redirect to an HTML page would reach the dashboard's fetch()
        as a 200 full of markup, and the UI would report a puzzling
        success. An API says 401."""
        r = client.post("/api/tournament/pay/5Abc")
        assert r.status_code == 401
        assert r.json()["detail"]

    def test_the_login_page_itself_is_reachable(self, client):
        r = client.get("/login")
        assert r.status_code == 200
        assert "Masuk" in r.text

    def test_a_logged_in_user_reaches_the_page(self, client):
        assert _login(client).status_code == 303
        r = client.get("/")
        assert r.status_code == 200
        assert r.json() == {"page": "dashboard"}


class TestLoggingIn:
    def test_the_right_password_sets_a_session_cookie(self, client):
        r = _login(client)
        cookie = r.cookies.get(sessions.COOKIE_NAME)
        assert sessions.read(cookie, SECRET) == "admin"

    def test_the_cookie_cannot_be_read_by_javascript_or_sent_cross_site(
        self, client
    ):
        """It is the only thing standing between a visitor and the
        wallets; a script-readable or cross-site-sent cookie hands it to
        the first XSS or forged form that comes along."""
        header = _login(client).headers["set-cookie"].lower()
        assert "httponly" in header
        assert "secure" in header
        assert "samesite=strict" in header

    def test_a_wrong_password_is_refused(self, client):
        r = _login(client, password="salah")
        assert r.status_code == 401
        assert sessions.COOKIE_NAME not in r.cookies
        assert "salah" in r.text.lower()

    def test_an_unknown_user_is_refused_the_same_way(self, client):
        """Same wording as a wrong password: telling a stranger which
        usernames exist is half the work of guessing one."""
        wrong_pw = _login(client, password="salah").text
        unknown = _login(client, username="hantu").text
        assert "Nama pengguna atau kata sandi salah." in wrong_pw
        assert "Nama pengguna atau kata sandi salah." in unknown

    def test_next_returns_the_visitor_where_they_were_going(self, client):
        r = _login(client, next="/kas?limit=5")
        assert r.headers["location"] == "/kas?limit=5"

    def test_next_cannot_point_off_site(self, client):
        """Otherwise the team's own login page becomes a convincing
        redirector into somebody else's."""
        for hostile in ("https://evil.example/x", "//evil.example/x"):
            r = _login(client, next=hostile)
            assert r.headers["location"] == "/"


class TestLoggingOut:
    def test_logout_clears_the_cookie_and_the_session_stops_working(self, client):
        _login(client)
        assert client.get("/").status_code == 200

        r = client.post("/logout")
        assert r.status_code == 200
        assert "sudah keluar" in r.text.lower()

        assert client.get("/").status_code == 303


def test_logout_says_so_even_while_basic_auth_is_still_in_front(app, client):
    """The click that looked broken. With nginx Basic Auth still there
    and no users configured yet, redirecting to /login bounced straight
    back to the dashboard — indistinguishable from a dead button. And
    the app genuinely cannot clear those credentials, so it has to say
    who can: the browser."""
    app.state.config.auth = AuthConfig()
    r = client.post("/logout", headers={"X-Remote-User": "admin"})

    assert r.status_code == 200
    assert "browser" in r.text.lower()


class TestSessionsAreNotForgeable:
    def test_a_cookie_signed_with_another_secret_is_ignored(self, client):
        forged = sessions.issue("admin", "not-the-secret", ttl_seconds=3600)
        client.cookies.set(sessions.COOKIE_NAME, forged)
        assert client.get("/").status_code == 303

    def test_an_expired_cookie_is_ignored(self, client):
        stale = sessions.issue("admin", SECRET, ttl_seconds=-1)
        client.cookies.set(sessions.COOKIE_NAME, stale)
        assert client.get("/").status_code == 303

    def test_removing_a_user_from_the_config_logs_them_out(self, app, client):
        """Revocation has to work on the session already issued, not just
        on the next login — otherwise someone who leaves keeps access for
        a week."""
        _login(client)
        assert client.get("/").status_code == 200

        # Another user stays: emptying the table entirely means "no
        # login configured", which is a different case (see below).
        app.state.config.auth.users["susnet"] = hash_password("lain")
        app.state.config.auth.users.pop("admin")
        assert client.get("/").status_code == 303


class TestCrossOriginPosts:
    def test_a_post_from_another_site_is_refused(self, client):
        _login(client)
        r = client.post(
            "/api/tournament/pay/5Abc",
            headers={"Origin": "https://evil.example"},
        )
        assert r.status_code == 403

    def test_the_sites_own_post_goes_through(self, client):
        _login(client)
        r = client.post(
            "/api/tournament/pay/5Abc",
            headers={"Origin": "https://testserver"},
        )
        assert r.status_code == 200


class TestWhenNoLoginIsConfigured:
    def test_the_gate_stays_open_so_an_upgrade_cannot_lock_everyone_out(
        self, app, client
    ):
        """A deployment still fronted by Basic Auth has no users here.
        Enforcing then would mean nobody can get in, with no way to fix
        it from the browser."""
        app.state.config.auth = AuthConfig()
        assert client.get("/").status_code == 200


class TestThrottling:
    def test_guessing_is_shut_off_after_repeated_failures(self, client):
        for _ in range(10):
            assert _login(client, password="salah").status_code == 401
        blocked = _login(client, password="salah")
        assert blocked.status_code == 429
        # And the correct password does not slip past the throttle either.
        assert _login(client).status_code == 429

    def test_the_window_moves_so_a_locked_out_user_recovers(self):
        now = [1000.0]
        throttle = LoginThrottle(clock=lambda: now[0])
        for _ in range(10):
            throttle.record_failure("1.2.3.4")
        assert throttle.blocked("1.2.3.4")

        now[0] += 301
        assert not throttle.blocked("1.2.3.4")


class TestPasswordStorage:
    def test_the_stored_form_is_not_the_password(self):
        stored = hash_password(PASSWORD)
        assert PASSWORD not in stored
        assert verify_password(PASSWORD, stored)
        assert not verify_password(PASSWORD + "x", stored)

    def test_the_same_password_hashes_differently_every_time(self):
        """Per-password salt: without it, two people with the same
        password are visibly the same in the config file, and one
        precomputed table cracks both."""
        assert hash_password(PASSWORD) != hash_password(PASSWORD)

    def test_a_corrupt_stored_hash_fails_the_login_rather_than_the_site(self):
        for broken in ("", "plaintext", "scrypt$nonsense", "bcrypt$x$y$z$a$b"):
            assert verify_password(PASSWORD, broken) is False


def test_the_proxy_header_is_ignored_once_our_own_login_exists(client):
    """uvicorn listens on localhost, and this host serves another site
    too. Any local process can send X-Remote-User: admin — which was
    acceptable only while nginx was the thing doing the authenticating.
    With a login of our own, accepting it would make the session cookie
    decorative and hand the wallet buttons to whatever else runs here."""
    r = client.get("/", headers={"X-Remote-User": "admin"})
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")

    r = client.post(
        "/api/tournament/pay/5Abc", headers={"X-Remote-User": "admin"}
    )
    assert r.status_code == 401
