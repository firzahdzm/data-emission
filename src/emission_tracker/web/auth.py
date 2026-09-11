"""Read the authenticated user from nginx (`X-Remote-User` header) and
gate admin-only endpoints against the `admin_users` config list."""

import hmac
import os

from fastapi import HTTPException, Request

PROXY_SECRET_HEADER = "X-Auth-Proxy"


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


def current_user(request: Request) -> str | None:
    """Return the authenticated username forwarded by nginx, or None when
    no auth layer is in front (local dev, tests).

    Dev escape hatch: if EMISSION_DEV_USER is set, treat all requests as
    that user. Lets you test admin UI locally without setting up nginx +
    Basic Auth. Never use in production.
    """
    dev_user = os.environ.get("EMISSION_DEV_USER")
    if dev_user:
        return dev_user
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
