"""Read the authenticated user from nginx (`X-Remote-User` header) and
gate admin-only endpoints against the `admin_users` config list."""

import hmac
import logging
import os

from fastapi import HTTPException, Request

from emission_tracker.web import sessions

log = logging.getLogger(__name__)

PROXY_SECRET_HEADER = "X-Auth-Proxy"


def _proxy_secret_configured(request: Request) -> bool:
    config = getattr(request.app.state, "config", None)
    return bool(getattr(config, "proxy_secret", None)) if config else False


def proxy_secret_ok(request: Request) -> bool:
    """True when the request carries the secret nginx adds, or when no
    secret is configured.

    uvicorn listens on localhost, so `X-Remote-User` on its own proves
    nothing: any process on the host can set it. nginx is the only party
    that knows the secret, so its presence is what makes the forwarded
    username trustworthy. Configuring no secret keeps the old behaviour,
    so an existing deployment does not lock itself out on upgrade.
    """
    config = getattr(request.app.state, "config", None)
    expected = getattr(config, "proxy_secret", None) if config else None
    if not expected:
        return True
    presented = request.headers.get(PROXY_SECRET_HEADER) or ""
    return hmac.compare_digest(presented, expected)


def _auth_config(request: Request):
    config = getattr(request.app.state, "config", None)
    return getattr(config, "auth", None) if config else None


def _own_login_configured(request: Request) -> bool:
    auth = _auth_config(request)
    return bool(auth and auth.users and auth.session_secret)


def session_user(request: Request) -> str | None:
    """The user named by a valid session cookie, or None.

    Checked before the proxy header: the login page is the tracker's own
    now, and a session it issued is the strongest evidence it has. The
    name must still be one the config knows — removing someone from
    `auth.users` has to log them out, not merely stop them logging in.
    """
    auth = _auth_config(request)
    if not auth or not auth.session_secret:
        return None
    user = sessions.read(
        request.cookies.get(sessions.COOKIE_NAME), auth.session_secret
    )
    if user is None or user not in auth.users:
        return None
    return user


def current_user(request: Request) -> str | None:
    """Return the authenticated username forwarded by nginx, or None when
    no auth layer is in front (local dev, tests).

    Dev escape hatch: if EMISSION_DEV_USER is set, treat all requests as
    that user. Lets you test admin UI locally without setting up nginx +
    Basic Auth. Never use in production — and it is ignored outright when
    `proxy_secret` is configured.
    """
    from_session = session_user(request)
    if from_session:
        return from_session

    dev_user = os.environ.get("EMISSION_DEV_USER")
    if dev_user:
        if _proxy_secret_configured(request):
            # A deployment that configured the proxy secret has declared it
            # is not a dev box. Honouring the escape hatch there would let
            # anything that can set one environment variable on the host
            # become an admin — which now means spending from the wallets.
            log.warning(
                "ignoring EMISSION_DEV_USER=%r: proxy_secret is configured, "
                "so this is not a dev deployment",
                dev_user,
            )
        else:
            return dev_user
    if _own_login_configured(request):
        # The proxy header is not consulted at all once the tracker runs
        # its own login. uvicorn listens on localhost, so any other
        # process on this host can send X-Remote-User: admin — and there
        # are others; the box also serves a second site. While nginx did
        # the authenticating that header was the only evidence available.
        # Now it is a weaker duplicate of evidence we already have, and
        # the weakest accepted proof is the one that decides.
        return None
    if not proxy_secret_ok(request):
        return None
    return request.headers.get("X-Remote-User") or None


def is_admin(request: Request) -> bool:
    """True if the request's user is in the configured admin_users list."""
    user = current_user(request)
    if not user:
        return False
    config = getattr(request.app.state, "config", None)
    admins = getattr(config, "admin_users", []) if config else []
    return user in admins


def require_admin(request: Request) -> str:
    """FastAPI dependency: 403s the request if the user isn't an admin.
    Returns the username on success so handlers can audit-log it."""
    user = current_user(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Not authenticated (X-Remote-User header missing)",
        )
    if not is_admin(request):
        raise HTTPException(
            status_code=403,
            detail=f"User {user!r} is not an admin",
        )
    return user
