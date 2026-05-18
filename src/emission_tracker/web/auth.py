"""Read the authenticated user from nginx (`X-Remote-User` header) and
gate admin-only endpoints against the `admin_users` config list."""

from fastapi import HTTPException, Request


def current_user(request: Request) -> str | None:
    """Return the authenticated username forwarded by nginx, or None when
    no auth layer is in front (local dev, tests)."""
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
