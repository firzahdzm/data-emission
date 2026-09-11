"""Password hashing for the tracker's own login page.

scrypt from the standard library rather than bcrypt or argon2: this is a
team of eleven behind one login, and adding a compiled dependency to the
deploy for it would buy nothing. The parameters below are the interactive
set from RFC 7914 — roughly 100ms per verification on the target host,
which is a wall for guessing and invisible to a person logging in.

Hashes are stored in the config file, never plaintext, so the file being
readable does not hand over the login.
"""

import base64
import hashlib
import hmac
import os

# RFC 7914 interactive parameters. maxmem must be raised alongside n:
# hashlib's default (32 MiB) is below what n=16384, r=8 needs.
_N = 16384
_R = 8
_P = 1
_MAXMEM = 64 * 1024 * 1024
_PREFIX = "scrypt"


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.scrypt(
        password.encode(), salt=salt, n=_N, r=_R, p=_P, maxmem=_MAXMEM, dklen=32
    )
    return "$".join(
        [
            _PREFIX,
            str(_N),
            str(_R),
            str(_P),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash.

    Returns False for anything malformed rather than raising: a corrupt
    line in the config must fail one login, not take the site down.
    """
    try:
        prefix, n, r, p, salt_b64, digest_b64 = (stored or "").split("$")
        if prefix != _PREFIX:
            return False
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p), maxmem=_MAXMEM, dklen=len(expected),
        )
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


if __name__ == "__main__":  # pragma: no cover - operator tool
    import getpass

    value = getpass.getpass("Password: ")
    if value != getpass.getpass("Repeat:   "):
        raise SystemExit("they do not match")
    print(hash_password(value))
