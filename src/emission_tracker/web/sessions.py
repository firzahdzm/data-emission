"""Signed session cookies.

A cookie the server can verify without storing anything: the username and
an expiry, signed with a secret only the server knows. No session table
to keep, and a restart does not log everyone out — as long as the secret
is configured rather than generated.

The signature is over the payload *including* the expiry, so a client
cannot extend its own session, and an expired cookie is rejected even
though its signature is still valid.
"""

import base64
import hmac
import time
from hashlib import sha256

COOKIE_NAME = "sn_session"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(payload: str, secret: str) -> str:
    return _b64(hmac.new(secret.encode(), payload.encode(), sha256).digest())


def issue(username: str, secret: str, *, ttl_seconds: int, now=time.time) -> str:
    payload = f"{_b64(username.encode())}.{int(now()) + ttl_seconds}"
    return f"{payload}.{_sign(payload, secret)}"


def read(cookie: str | None, secret: str, *, now=time.time) -> str | None:
    """The username in a valid, unexpired cookie — otherwise None.

    Anything unparseable is simply "not logged in". A malformed cookie is
    indistinguishable from a forged one from here, and both deserve the
    same answer.
    """
    if not cookie or not secret:
        return None
    try:
        user_b64, expiry, signature = cookie.split(".")
        payload = f"{user_b64}.{expiry}"
        if not hmac.compare_digest(signature, _sign(payload, secret)):
            return None
        if int(expiry) <= int(now()):
            return None
        return _unb64(user_b64).decode()
    except Exception:
        return None
