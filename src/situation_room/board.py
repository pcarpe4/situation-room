"""What the board shows. Read-only, and a fixed number of queries however many rows there are."""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from . import dates
from .clock import Clock
from .models import Assignment, Firedrill, FiredrillState, Person, Status, Update, UpdateRequest
from .policy import Action, can

ROLLUP_ORDER = (Status.BLOCKED, Status.AT_RISK, Status.NEW, Status.ON_TRACK, Status.DONE)
STATUS_LABELS = {
    Status.NEW: "No update yet",
    Status.ON_TRACK: "On track",
    Status.AT_RISK: "At risk",
    Status.BLOCKED: "Blocked",
    Status.DONE: "Done",
}
FILTERS = ("open", "stale", "asked", "waiting", "due", "overdue")


@dataclass(frozen=True)
class OpenAsk:
    id: int
    requested_at: datetime
    confirmed_sent_at: datetime | None
    asked_by_id: int

    @property
    def sent(self) -> bool:
        """The asker confirmed they emailed it."""
        return self.confirmed_sent_at is not None


class AskState(enum.StrEnum):
    """Where a line's asks stand, as this viewer should see them."""

    NONE = "none"
    MINE_UNSENT = "mine_unsent"  # the viewer recorded an ask and has not confirmed emailing it
    SENT = "sent"  # an ask was emailed and has no reply yet
    OTHERS_UNSENT = "others_unsent"  # someone else recorded an ask and has not confirmed emailing it
    WAITING = "waiting"  # no sent ask, and the owner is waiting on a date


class LineAction(enum.StrEnum):
    CONFIRM_SENT = "confirm_sent"
    CANCEL_ASK = "cancel_ask"
    ASK_AGAIN = "ask_again"
    ASK_ANYWAY = "ask_anyway"
    REQUEST = "request"
    UPDATE = "update"
    POST_FOR = "post_for"


@dataclass(frozen=True)
class LineView:
    ask_state: AskState
    ask: OpenAsk | None  # the ask `ask_state` is about
    actions: tuple[LineAction, ...]
    primary: LineAction | None


def line_view(viewer: Person, assignment: Assignment, *, has_update: bool, waiting: bool,
              asks: list[OpenAsk]) -> LineView:
    """What `viewer` sees and may do on one owner line. `asks` are its open asks, newest first.

    Role questions go to `policy`; templates only render the result.
    """
    is_owner = assignment.owner_id == viewer.id
    may_post = can(viewer, Action.POST_UPDATE, assignment)
    if assignment.status == Status.DONE:
        return LineView(AskState.NONE, None, (LineAction.UPDATE,) if is_owner and may_post else (), None)

    mine = next((a for a in asks if a.asked_by_id == viewer.id and not a.sent), None)
    sent = next((a for a in asks if a.sent), None)
    if mine:
        state, ask = AskState.MINE_UNSENT, mine
    elif sent:
        state, ask = AskState.SENT, sent
    elif asks:
        state, ask = AskState.OTHERS_UNSENT, asks[0]
    elif waiting:
        state, ask = AskState.WAITING, None
    else:
        state, ask = AskState.NONE, None

    actions: list[LineAction] = []
    primary: LineAction | None = None
    if can(viewer, Action.REQUEST_UPDATE):
        if state == AskState.MINE_UNSENT:
            actions += [LineAction.CONFIRM_SENT, LineAction.CANCEL_ASK]
        elif state in (AskState.SENT, AskState.OTHERS_UNSENT):
            actions.append(LineAction.ASK_AGAIN)
        elif state == AskState.WAITING:
            actions.append(LineAction.ASK_ANYWAY)
        else:
            actions.append(LineAction.REQUEST)
            if assignment.status == Status.NEW and not has_update:
                primary = LineAction.REQUEST
    if may_post:
        if is_owner:
            actions.append(LineAction.UPDATE)
            primary = LineAction.UPDATE
        else:
            actions.append(LineAction.POST_FOR)
    return LineView(state, ask, tuple(actions), primary)


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
    asked: bool  # an emailed ask is open: what "Asked, no reply" counts
    ask_state: AskState
    ask: OpenAsk | None
    actions: tuple[LineAction, ...]
    primary: LineAction | None

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


def _load(s: Session, clock: Clock, stale_after: int, viewer: Person, owner_id: int | None = None,
          public_id: uuid.UUID | None = None) -> list[Row]:
    today = clock.today()
    # No holidays are configured yet; when they are, they go in here.
    rules = dates.StaleRules(tz=clock.tz, stale_after=stale_after)
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
    asks: dict[int, list[OpenAsk]] = {}
    if assignment_ids:
        for u in s.scalars(
            select(Update).where(Update.assignment_id.in_(assignment_ids))
            .ext(distinct_on(Update.assignment_id)).order_by(Update.assignment_id, Update.id.desc())
        ):
            latest[u.assignment_id] = u
        # Every open ask, newest first: one viewer's unsent ask must not hide behind someone else's.
        for r in s.scalars(
            select(UpdateRequest)
            .where(UpdateRequest.assignment_id.in_(assignment_ids), UpdateRequest.is_open)
            .order_by(UpdateRequest.assignment_id, UpdateRequest.id.desc())
        ):
            asks.setdefault(r.assignment_id, []).append(
                OpenAsk(r.id, r.requested_at, r.confirmed_sent_at, r.asked_by_id))

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
        active = a.status != Status.DONE
        state = dates.staleness(created_at=a.created_at, newest_update_at=u.created_at if u else None,
                                waiting_until=a.waiting_until, done=not active, today=today, rules=rules)
        open_asks = asks.get(a.id, []) if active else []
        view = line_view(viewer, a, has_update=u is not None, waiting=state.waiting, asks=open_asks)
        rows[a.firedrill_id].lines.append(OwnerLine(
            assignment_id=a.public_id, owner_id=person.id, owner_name=person.display_name, owner_email=person.email,
            assigned_at=a.created_at, status=a.status, last_update_text=u.text if u else None, last_update_at=u.created_at if u else None,
            last_update_posted_for=bool(u and u.on_behalf_of_id), waiting_until=a.waiting_until,
            waiting_reason=a.waiting_reason, waiting=state.waiting, stale=state.stale, stale_days=state.stale_days,
            asked=any(k.sent for k in open_asks),
            ask_state=view.ask_state, ask=view.ask, actions=view.actions, primary=view.primary,
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
        "asked": line.asked,
        "waiting": line.waiting,
    }[active_filter]


def build(s: Session, clock: Clock, stale_after: int, viewer: Person, active_filter: str = "open") -> Board:
    rows = _load(s, clock, stale_after, viewer)
    counts = {
        "open": len(rows),
        "stale": sum(line.stale for r in rows for line in r.lines),
        "asked": sum(line.asked for r in rows for line in r.lines),
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


def mine(s: Session, clock: Clock, stale_after: int, viewer: Person, person_id: int | None = None) -> list[Row]:
    """The firedrills a person (the viewer, unless named) owns a line on, with their own line first."""
    person_id = viewer.id if person_id is None else person_id
    rows = _load(s, clock, stale_after, viewer, owner_id=person_id)
    for r in rows:
        r.lines.sort(key=lambda line: (line.owner_id != person_id, line.sort_key))
    return rows


def one(s: Session, clock: Clock, stale_after: int, viewer: Person, public_id: uuid.UUID) -> Row | None:
    """The board row for one open firedrill, or None when it is closed or has no owners."""
    rows = _load(s, clock, stale_after, viewer, public_id=public_id)
    return rows[0] if rows else None


def _plural(n: int, word: str) -> str:
    return word if n == 1 else word + "s"
