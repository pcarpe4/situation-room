"""The M1 ask: a mailto link the asker sends from their own mail program."""

from __future__ import annotations

from datetime import date
from urllib.parse import quote

from .models import Status

STATUS_LINKS: tuple[tuple[Status, str], ...] = (
    (Status.ON_TRACK, "On track"),
    (Status.AT_RISK, "At risk"),
    (Status.BLOCKED, "Blocked"),
    (Status.DONE, "Done"),
)
# Mail clients truncate long mailto links. Held under this until a real limit is measured.
BUDGET = 1800


def _one_line(text: str) -> str:
    return " ".join((text or "").split())


def update_link(public_url: str, assignment_public_id: object, status: Status | None = None) -> str:
    link = f"{public_url}/a/{assignment_public_id}/update"
    return f"{link}?status={status.value}" if status else link


def ask_mailto(
    *, public_url: str, to_email: str, title: str, due_date: date | None,
    assignment_public_id: object, asker_name: str,
) -> str:
    title = _one_line(title)
    due = f" (due {due_date.strftime('%b')} {due_date.day})" if due_date else ""
    subject = f"Update needed: {title}{due}"
    links = "\n".join(
        f"{label} (opens a short form): {update_link(public_url, assignment_public_id, status)}"
        for status, label in STATUS_LINKS
    )
    body = (
        f"{_one_line(asker_name)} is asking for an update on: {title}\n\n"
        f"Pick the line that fits and add a sentence:\n\n{links}\n\n"
        f"A reply to this email reaches {_one_line(asker_name)}, not the board. Use a link above."
    )
    url = _build(to_email, subject, body)
    if len(url) <= BUDGET:
        return url
    short_body = (
        f"{_one_line(asker_name)} is asking for an update on: {title}\n\n"
        f"Update it here: {update_link(public_url, assignment_public_id)}"
    )
    return _build(to_email, subject, short_body)


def _build(to_email: str, subject: str, body: str) -> str:
    return (
        "mailto:" + quote(to_email, safe="@")
        + "?subject=" + quote(subject, safe="")
        + "&body=" + quote(body, safe="")
    )
