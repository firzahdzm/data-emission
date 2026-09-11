"""The tracker's own login page, and the gate that requires it.

Replaces nginx Basic Auth. That had two problems no amount of styling
fixes: there is no way to log out of it — the browser holds the
credentials until every window is closed — and the prompt appears before
the site is ever seen, from the browser's chrome rather than the app.

The gate lives here rather than in nginx so that "who is logged in" has
exactly one answer, and so a misconfigured proxy cannot quietly open the
site: with `auth.users` configured, an unauthenticated request never
reaches a page handler.
"""

import logging
import time
from collections import deque
from urllib.parse import parse_qsl, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from emission_tracker.web import sessions
from emission_tracker.web.auth import current_user
from emission_tracker.web.passwords import verify_password

log = logging.getLogger(__name__)

# Paths that must work without a session, or nobody could ever log in.
PUBLIC_PREFIXES = ("/login", "/logout", "/static", "/healthz", "/favicon")

# Guessing a password over the network should be hopeless, not merely
# slow. Ten failures from one address inside five minutes and that
# address waits — the team logs in from a handful of places, so this is
# invisible to them and fatal to a script.
_MAX_FAILURES = 10
_WINDOW_SECONDS = 300


class LoginThrottle:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures: dict[str, deque] = {}

    def blocked(self, key: str) -> bool:
        attempts = self._prune(key)
        return len(attempts) >= _MAX_FAILURES

    def record_failure(self, key: str) -> None:
        self._prune(key).append(self._clock())

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)

    def _prune(self, key: str) -> deque:
        attempts = self._failures.setdefault(key, deque())
        cutoff = self._clock() - _WINDOW_SECONDS
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        return attempts


def _auth(request: Request):
    config = getattr(request.app.state, "config", None)
    return getattr(config, "auth", None) if config else None


def login_required(request: Request) -> bool:
    """Whether this deployment gates on the tracker's own login.

    A deployment with no users configured is one still fronted by Basic
    Auth (or a dev box). Turning the gate on there would lock everyone
    out on upgrade, so the absence of configuration means "not mine to
    enforce" — never "let everyone in", because nginx is still asking.
    """
    auth = _auth(request)
    return bool(auth and auth.users and auth.session_secret)


def _safe_next(raw: str | None) -> str:
    """Only same-site paths. `next=https://evil.example` on a login link
    is how a login page becomes a redirector for somebody else."""
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    parts = urlsplit(raw)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _is_https(request: Request) -> bool:
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0]
    return (forwarded.strip() or request.url.scheme) == "https"


def _same_origin(request: Request) -> bool:
    """Guard state-changing requests that ride on the session cookie.

    SameSite=Strict already keeps the cookie off cross-site requests;
    this is the second lock, for the browser or edge case where it does
    not. A request with no Origin at all is same-origin by omission —
    that is what curl and the app's own server-side calls look like.
    """
    origin = request.headers.get("origin")
    if not origin:
        return True
    host = request.headers.get("host")
    return urlsplit(origin).netloc == host


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="id" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Masuk · SUSnet Emission Tracker</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap">
<link rel="stylesheet" href="/static/style.css?v={version}">
</head>
<body class="login-body">
<main class="login-card">
    <img src="/static/logo-white.svg" alt="SUSnet" class="login-logo">
    <p class="login-sub">Bittensor subnet 56 · team performance monitor</p>
    {error}
    <form method="post" action="/login">
        <input type="hidden" name="next" value="{next}">
        <label for="username">Nama pengguna</label>
        <input id="username" name="username" autocomplete="username" autofocus required>
        <label for="password">Kata sandi</label>
        <input id="password" name="password" type="password"
               autocomplete="current-password" required>
        <button type="submit">Masuk</button>
    </form>
</main>
</body>
</html>
"""


def _render_login(request: Request, next_url: str, error: str = "", status=200):
    from emission_tracker.web.routes_pages import _asset_version

    block = f'<p class="login-error">{error}</p>' if error else ""
    html = (
        LOGIN_PAGE.replace("{version}", _asset_version())
        .replace("{error}", block)
        .replace("{next}", _safe_next(next_url))
    )
    return HTMLResponse(html, status_code=status)


LOGGED_OUT_PAGE = """<!DOCTYPE html>
<html lang="id" data-theme="dark">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Keluar · SUSnet Emission Tracker</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600&display=swap">
<link rel="stylesheet" href="/static/style.css?v={version}">
</head>
<body class="login-body">
<main class="login-card">
    <img src="/static/logo-white.svg" alt="SUSnet" class="login-logo">
    <p class="login-sub">{message}</p>
    <a href="/login" role="button" style="display:block;text-align:center;">Masuk lagi</a>
</main>
</body>
</html>
"""


def _render_logged_out(request: Request, message: str) -> HTMLResponse:
    from emission_tracker.web.routes_pages import _asset_version
    from html import escape

    html = (
        LOGGED_OUT_PAGE.replace("{version}", _asset_version())
        .replace("{message}", escape(message))
    )
    return HTMLResponse(html)


def register_auth(app: FastAPI) -> None:
    throttle = LoginThrottle()
    app.state.login_throttle = throttle

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request, next: str = "/"):
        if current_user(request):
            return RedirectResponse(_safe_next(next), status_code=303)
        return _render_login(request, next)

    @app.post("/login")
    async def login_submit(request: Request):
        # The body is parsed here rather than with FastAPI's Form(...) or
        # request.form(): both route through python-multipart, a
        # dependency this deployment would have to install for an
        # ordinary urlencoded login form. The body is capped first — an
        # unbounded read on an unauthenticated endpoint is a free way to
        # make the process allocate.
        raw = (await request.body())[:8192].decode("utf-8", "replace")
        form = dict(parse_qsl(raw, keep_blank_values=True))
        username = form.get("username", "")
        password = form.get("password", "")
        next = form.get("next", "/")
        auth = _auth(request)
        if not login_required(request):
            return RedirectResponse(_safe_next(next), status_code=303)

        client = request.client.host if request.client else "unknown"
        if throttle.blocked(client):
            log.warning("login throttled for %s", client)
            return _render_login(
                request, next,
                "Terlalu banyak percobaan. Coba lagi beberapa menit lagi.",
                status=429,
            )

        stored = auth.users.get(username)
        # verify_password is run even for an unknown username so the reply
        # takes the same time either way; otherwise the login page tells a
        # stranger which names exist.
        ok = verify_password(password, stored or "scrypt$16384$8$1$AA==$AA==")
        if not stored or not ok:
            throttle.record_failure(client)
            log.warning("failed login for %r from %s", username, client)
            return _render_login(
                request, next, "Nama pengguna atau kata sandi salah.",
                status=401,
            )

        throttle.clear(client)
        log.info("login ok for %r from %s", username, client)
        response = RedirectResponse(_safe_next(next), status_code=303)
        response.set_cookie(
            sessions.COOKIE_NAME,
            sessions.issue(
                username, auth.session_secret,
                ttl_seconds=auth.session_hours * 3600,
            ),
            max_age=auth.session_hours * 3600,
            httponly=True,          # JavaScript must never read it
            # Secure on the real site; off only when the request itself
            # arrived over plain http, which in practice means a laptop
            # running the app directly. Hardcoding True there would issue
            # a cookie the browser refuses to send back, and the login
            # would appear to do nothing.
            secure=_is_https(request),
            samesite="strict",      # keeps the cookie off cross-site posts
            path="/",
        )
        return response

    @app.post("/logout")
    @app.get("/logout")
    def logout(request: Request):
        # A page, not a redirect to /login. Redirecting looks broken in
        # the one case that matters most: while nginx Basic Auth is still
        # in front, the browser re-sends its credentials, /login sees an
        # authenticated visitor and bounces straight back to the
        # dashboard — the click appears to do nothing at all. A page says
        # what happened, and says the part the app cannot do anything
        # about: only the browser can forget Basic Auth credentials.
        still_basic_auth = not login_required(request) and bool(
            request.headers.get("x-remote-user")
        )
        message = (
            "Sesi di aplikasi sudah dihapus. Tapi situs ini masih dijaga "
            "login bawaan browser, dan hanya browser yang bisa melupakan "
            "itu — tutup semua jendela browser untuk keluar sepenuhnya."
            if still_basic_auth
            else "Kamu sudah keluar."
        )
        response = _render_logged_out(request, message)
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return response

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        path = request.url.path
        if not login_required(request) or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        if current_user(request) is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "not authenticated"}, status_code=401)
            target = request.url.path
            if request.url.query:
                target += f"?{request.url.query}"
            return RedirectResponse(f"/login?next={target}", status_code=303)

        if request.method not in ("GET", "HEAD", "OPTIONS") \
                and not _same_origin(request):
            log.warning("cross-origin %s %s refused", request.method, path)
            return JSONResponse({"detail": "cross-origin request refused"},
                                status_code=403)

        return await call_next(request)
