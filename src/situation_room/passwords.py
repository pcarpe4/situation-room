"""Passwords for local profiles, and the one-time links that set them.

Hashing is scrypt from the standard library. A password link is a signed,
expiring token that also carries a fingerprint of the person's current
password, so it stops working the moment a new password is set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from itsdangerous import BadData, URLSafeTimedSerializer

MIN_LENGTH = 10
MAX_LENGTH = 200
LINK_MAX_AGE = 3 * 24 * 60 * 60

_N_LOG2, _R, _P = 15, 8, 1
_MAXMEM = 64 * 1024 * 1024
_KEY_LENGTH = 32


def _derive(password: str, salt: bytes, n_log2: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=1 << n_log2, r=r, p=p,
                          maxmem=_MAXMEM, dklen=_KEY_LENGTH)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = _derive(password, salt, _N_LOG2, _R, _P)
    return "$".join(("scrypt", str(_N_LOG2), str(_R), str(_P),
                     base64.b64encode(salt).decode(), base64.b64encode(key).decode()))


_DUMMY: str | None = None


def verify_password(password: str, stored: str | None) -> bool:
    """False for a wrong password and for a person with none. Takes the same time either way."""
    global _DUMMY
    if stored is None:
        if _DUMMY is None:
            _DUMMY = hash_password(secrets.token_urlsafe(16))
        stored, known = _DUMMY, False
    else:
        known = True
    try:
        scheme, n_log2, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        found = _derive(password[:MAX_LENGTH], base64.b64decode(salt), int(n_log2), int(r), int(p))
        return hmac.compare_digest(found, base64.b64decode(key)) and known
    except ValueError:
        return False


def fingerprint(stored: str | None) -> str:
    """Changes whenever the password does. Sessions and password links carry it."""
    return hashlib.sha256((stored or "").encode()).hexdigest()[:16]


def _signer(secret_key: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret_key, salt="sr-password-link")


def link_token(secret_key: str, person_id: int, stored: str | None) -> str:
    return _signer(secret_key).dumps({"p": person_id, "f": fingerprint(stored)})


def read_link_token(secret_key: str, token: str) -> tuple[int, str] | None:
    """The person id and password fingerprint a token was made for, or None if it is bad or old."""
    try:
        data = _signer(secret_key).loads(token, max_age=LINK_MAX_AGE)
        return int(data["p"]), str(data["f"])
    except (BadData, KeyError, TypeError, ValueError):
        return None
