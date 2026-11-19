"""Sessions, sign-in state and request-forgery protection."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Iterator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .. import passwords
from ..models import Person

# Set once a browser has signed in. Without it a visitor is new here and is offered a profile.
KNOWN_COOKIE = "sr_known"
STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class SignInRequired(Exception):
    def __init__(self, next_path: str):
        self.next_path = next_path


class Refused(Exception):
    """Shown to the person as a plain page with a stable code."""

    def __init__(self, code: str, message: str, http_status: int = 403):
        self.code = code
        self.message = message
        self.http_status = http_status


def safe_next(value: str | None, default: str = "/") -> str:
    """Only a path on this site: starts with one slash, no scheme, no backslash."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return default
    return value


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf"] = token
    return token


async def csrf_protect(request: Request) -> None:
    """Applied to every route. Any state-changing request needs a matching token and origin."""
    if request.method not in STATE_CHANGING:
        return
    origin = request.headers.get("origin")
    if origin and origin != request.app.state.cfg.public_origin:
        raise Refused("CSRF_ORIGIN", "This request came from another site and was refused.")
    expected = request.session.get("csrf")
    form = await request.form()
    sent = form.get("csrf_token") or request.headers.get("x-csrf-token")
    if not expected or not isinstance(sent, str) or not hmac.compare_digest(sent, expected):
        raise Refused("CSRF_TOKEN", "This form has expired. Go back, reload the page and try again.")


class SignInThrottle:
    """Slows password guessing: after a run of wrong passwords for one email, that email waits.

    Kept in this process's memory, so each replica counts on its own.
    """

    def __init__(self, limit: int = 8, window_seconds: int = 600):
        self.limit = limit
        self.window = window_seconds
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        cutoff = time.monotonic() - self.window
        recent = [t for t in self._failures.get(key, ()) if t > cutoff]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked(self, email: str) -> bool:
        return len(self._recent(email.strip().lower())) >= self.limit

    def failed(self, email: str) -> None:
        key = email.strip().lower()[:320]
        if len(self._failures) > 10_000:
            self._failures.clear()
        self._failures.setdefault(key, []).append(time.monotonic())

    def succeeded(self, email: str) -> None:
        self._failures.pop(email.strip().lower(), None)


def start_session(request: Request, person: Person) -> None:
    """Sign this browser in as `person`. The session is tied to their current password."""
    request.session.clear()
    request.session["person_id"] = person.id
    request.session["pw"] = passwords.fingerprint(person.password_hash)


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as session:
        yield session


def current_person(request: Request, db: Session = Depends(get_db)) -> Person:
    person_id = request.session.get("person_id")
    path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    if not person_id:
        raise SignInRequired(safe_next(path))
    person = db.get(Person, person_id)
    if person is not None and request.session.get("pw") != passwords.fingerprint(person.password_hash):
        # The password changed since this browser signed in.
        request.session.pop("person_id", None)
        raise SignInRequired(safe_next(path))
    if person is None or not person.active:
        request.session.pop("person_id", None)
        raise Refused("NO_ACCESS", "Your roster entry is not active. Ask your Triage person to check the roster.")
    request.state.person = person
    return person
