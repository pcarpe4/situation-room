"""What the board shows. Read-only, and a fixed number of queries however many rows there are."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from . import dates
from .clock import Clock
from .models import Assignment, Firedrill, FiredrillState, Person, Status, Update, UpdateRequest

ROLLUP_ORDER = (Status.BLOCKED, Status.AT_RISK, Status.NEW, Status.ON_TRACK, Status.DONE)
STATUS_LABELS = {
    Status.NEW: "No update yet",
    Status.ON_TRACK: "On track",
    Status.AT_RISK: "At risk",
    Status.BLOCKED: "Blocked",
    Status.DONE: "Done",
}
FILTERS = ("open", "stale", "asked", "waiting", "due", "overdue")


@dataclass
class OpenAsk:
    id: int
    requested_at: datetime
    confirmed_sent_at: datetime | None
    asked_by_id: int


@dataclass
class OwnerLine:
    assignment_id: uuid.UUID
    owner_id: int
    owner_name: str
    owner_email: str
    assigned_at: datetime
    status: Status
    last_update_text: str | None
    last_update_at: datetime | None
    last_update_posted_for: bool
    waiting_until: date | None
    waiting_reason: str | None
    waiting: bool
    stale: bool
    stale_days: int
    open_ask: OpenAsk | None

    @property
    def sort_key(self) -> int:
        if self.stale:
            return 0
        if self.waiting:
            return 5
        return {Status.BLOCKED: 1, Status.AT_RISK: 2, Status.NEW: 3, Status.ON_TRACK: 4, Status.DONE: 6}[self.status]


@dataclass
class Row:
    public_id: uuid.UUID
    title: str
    short_description: str
    source: str | None
    due_date: date | None
    overdue_days: int
    due_this_week: bool
    ticket_refs: list[str]
    rollup: Status
    stale: bool
    ready_to_close: bool
    created_at: datetime
    lines: list[OwnerLine] = field(default_factory=list)
    hidden_owners: int = 0


@dataclass
class Board:
    rows: list[Row]
    counts: dict[str, int]
    active_filter: str
    result_line: str


def rollup(statuses: list[Status]) -> Status:
    """Worst status among assignments that have not ended. Done counts only when all are done."""
    not_done = [s for s in statuses if s != Status.DONE]
    if not not_done:
        return Status.DONE
    return min(not_done, key=ROLLUP_ORDER.index)


def _load(s: Session, clock: Clock, stale_after: int, owner_id: int | None = None,
          public_id: uuid.UUID | None = None) -> list[Row]:
    today = clock.today()
    query = select(Firedrill).where(Firedrill.state == FiredrillState.OPEN)
    if public_id is not None:
        query = query.where(Firedrill.public_id == public_id)
    firedrills = s.scalars(query).all()
    if not firedrills:
        return []
    by_id = {f.id: f for f in firedrills}

    assignments = s.execute(
        select(Assignment, Person)
        .join(Person, Person.id == Assignment.owner_id)
        .where(Assignment.firedrill_id.in_(by_id), Assignment.is_active)
        .order_by(Assignment.id)
    ).all()
    assignment_ids = [a.id for a, _ in assignments]

    latest: dict[int, Update] = {}
    asks: dict[int, OpenAsk] = {}
    if assignment_ids:
        for u in s.scalars(
            select(Update).where(Update.assignment_id.in_(assignment_ids))
            .ext(distinct_on(Update.assignment_id)).order_by(Update.assignment_id, Update.id.desc())
        ):
            latest[u.assignment_id] = u
        for r in s.scalars(
            select(UpdateRequest)
            .where(UpdateRequest.assignment_id.in_(assignment_ids), UpdateRequest.is_open)
            .ext(distinct_on(UpdateRequest.assignment_id))
            .order_by(UpdateRequest.assignment_id, UpdateRequest.id.desc())
        ):
            asks[r.assignment_id] = OpenAsk(r.id, r.requested_at, r.confirmed_sent_at, r.asked_by_id)

    rows: dict[int, Row] = {}
    for f in firedrills:
        overdue = dates.overdue_days(f.due_date, today)
        rows[f.id] = Row(
            public_id=f.public_id, title=f.title, short_description=f.short_description, source=f.source,
            due_date=f.due_date, overdue_days=overdue, due_this_week=dates.is_due_this_week(f.due_date, today),
            ticket_refs=f.ticket_refs.split() if f.ticket_refs else [], rollup=Status.NEW, stale=False,
            ready_to_close=False, created_at=f.created_at,
        )

    for a, person in assignments:
        u = latest.get(a.id)
        created = clock.local(a.created_at).date()
        newest = clock.local(u.created_at).date() if u else None
        waiting = dates.is_waiting(a.waiting_until, today)
        active = a.status != Status.DONE
        start = dates.clock_start(created, newest, a.waiting_until, today)
        stale = active and not waiting and dates.is_stale(start, today, stale_after)
        rows[a.firedrill_id].lines.append(OwnerLine(
            assignment_id=a.public_id, owner_id=person.id, owner_name=person.display_name, owner_email=person.email,
            assigned_at=a.created_at, status=a.status, last_update_text=u.text if u else None, last_update_at=u.created_at if u else None,
            last_update_posted_for=bool(u and u.on_behalf_of_id), waiting_until=a.waiting_until,
            waiting_reason=a.waiting_reason, waiting=waiting and active, stale=stale,
            stale_days=dates.stale_working_days(start, today) if stale else 0,
            open_ask=asks.get(a.id) if active else None,
        ))

    result = []
    for row in rows.values():
        if not row.lines:
            continue
        row.lines.sort(key=lambda line: line.sort_key)
        row.rollup = rollup([line.status for line in row.lines])
        row.stale = any(line.stale for line in row.lines)
        row.ready_to_close = row.rollup == Status.DONE
        if row.ready_to_close:
            row.overdue_days = 0
        if owner_id is not None and not any(line.owner_id == owner_id for line in row.lines):
            continue
        result.append(row)

    result.sort(key=lambda r: (
        0 if r.stale else 1, ROLLUP_ORDER.index(r.rollup), r.due_date or date.max, r.created_at,
    ))
    return result


def _line_matches(line: OwnerLine, active_filter: str) -> bool:
    return {
        "stale": line.stale,
        "asked": line.open_ask is not None,
        "waiting": line.waiting,
    }[active_filter]


def build(s: Session, clock: Clock, stale_after: int, active_filter: str = "open") -> Board:
    rows = _load(s, clock, stale_after)
    counts = {
        "open": len(rows),
        "stale": sum(line.stale for r in rows for line in r.lines),
        "asked": sum(line.open_ask is not None for r in rows for line in r.lines),
        "waiting": sum(line.waiting for r in rows for line in r.lines),
        "due": sum(r.due_this_week for r in rows),
        "overdue": sum(r.overdue_days > 0 for r in rows),
    }
    if active_filter not in FILTERS:
        active_filter = "open"

    shown = rows
    if active_filter in ("stale", "asked", "waiting"):
        shown = []
        for r in rows:
            matching = [line for line in r.lines if _line_matches(line, active_filter)]
            if matching:
                r.hidden_owners = len(r.lines) - len(matching)
                r.lines = matching
                shown.append(r)
        noun = {"stale": "stale", "asked": "asked with no reply", "waiting": "waiting"}[active_filter]
        result_line = f"{counts[active_filter]} {_plural(counts[active_filter], 'owner')} {noun} in {len(shown)} {_plural(len(shown), 'firedrill')}"
    elif active_filter == "due":
        shown = [r for r in rows if r.due_this_week]
        result_line = f"{len(shown)} {_plural(len(shown), 'firedrill')} due this week"
    elif active_filter == "overdue":
        shown = [r for r in rows if r.overdue_days > 0]
        result_line = f"{len(shown)} overdue {_plural(len(shown), 'firedrill')}"
    else:
        result_line = f"{len(rows)} open {_plural(len(rows), 'firedrill')}"
    return Board(rows=shown, counts=counts, active_filter=active_filter, result_line=result_line)


def mine(s: Session, clock: Clock, stale_after: int, person_id: int) -> list[Row]:
    """The firedrills a person owns a line on, with their own line first."""
    rows = _load(s, clock, stale_after, owner_id=person_id)
    for r in rows:
        r.lines.sort(key=lambda line: (line.owner_id != person_id, line.sort_key))
    return rows


def one(s: Session, clock: Clock, stale_after: int, public_id: uuid.UUID) -> Row | None:
    """The board row for one open firedrill, or None when it is closed or has no owners."""
    rows = _load(s, clock, stale_after, public_id=public_id)
    return rows[0] if rows else None


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"
