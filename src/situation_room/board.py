"""What the board shows. Read-only, and a fixed number of queries however many rows there are."""

from __future__ import annotations

import calendar as stdcal
import enum
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

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
FILTERS = ("open", "due", "overdue")


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
    ask_age_days: int = 0  # working days since `ask` was emailed (or recorded, if not emailed yet)

    @property
    def kind(self) -> str:
        """One word for the board's progress bar: what this owner's line looks like at a glance."""
        if self.status == Status.DONE:
            return "done"
        if self.waiting:
            return "waiting"
        if self.status in (Status.BLOCKED, Status.AT_RISK):
            return "risk"
        if self.stale:
            return "silent"
        if self.status == Status.NEW:
            return "fresh"
        return "on_track"

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

    @property
    def segments(self) -> list[str]:
        """One progress-bar segment per owner, done first and trouble last."""
        return sorted((line.kind for line in self.lines), key=SEGMENT_ORDER.index)

    @property
    def tally(self) -> dict[str, int]:
        kinds = [line.kind for line in self.lines]
        return {k: kinds.count(k) for k in SEGMENT_ORDER}


SEGMENT_ORDER = ("done", "on_track", "waiting", "fresh", "silent", "risk")


class NeedKind(enum.StrEnum):
    """Why an owner line is in the viewer's "Needs you" queue."""

    UPDATE = "update"  # the viewer owns the line and it needs their update
    ASK_AGAIN = "ask_again"  # the viewer's emailed ask has gone unanswered
    CONFIRM = "confirm"  # the viewer recorded an ask and has not said they emailed it
    REQUEST = "request"  # the owner has gone quiet and nobody has asked
    BLOCKED = "blocked"  # the owner is blocked and nobody has asked


NEED_ORDER = (NeedKind.UPDATE, NeedKind.ASK_AGAIN, NeedKind.CONFIRM, NeedKind.REQUEST, NeedKind.BLOCKED)


def need_of(row: Row, line: OwnerLine) -> NeedKind | None:
    """Whether this line needs the viewer now, and why. Reads only what `line_view` decided."""
    if line.status == Status.DONE:
        return None
    if LineAction.CONFIRM_SENT in line.actions:
        return NeedKind.CONFIRM
    if LineAction.UPDATE in line.actions and (line.stale or line.asked or row.overdue_days):
        return NeedKind.UPDATE
    if LineAction.ASK_AGAIN in line.actions and line.ask_state == AskState.SENT:
        # Give the owner a working day to answer an overdue firedrill, two otherwise.
        if line.ask_age_days >= (1 if row.overdue_days else 2):
            return NeedKind.ASK_AGAIN
        return None
    if LineAction.REQUEST in line.actions:
        if line.stale:
            return NeedKind.REQUEST
        if line.status == Status.BLOCKED:
            return NeedKind.BLOCKED
    return None


@dataclass(frozen=True)
class Need:
    kind: NeedKind
    row: Row
    line: OwnerLine


def needs(rows: list[Row]) -> list[Need]:
    """The viewer's queue: overdue firedrills first, then the quickest kind of action, then the longest quiet."""
    found = [Need(kind, r, line) for r in rows for line in r.lines if (kind := need_of(r, line))]
    found.sort(key=lambda n: (0 if n.row.overdue_days else 1, NEED_ORDER.index(n.kind), -n.line.stale_days,
                              n.row.due_date or date.max))
    return found


@dataclass(frozen=True)
class WeekDay:
    day: date
    is_today: bool
    is_past: bool
    due: list[Row]


@dataclass
class Board:
    rows: list[Row]
    counts: dict[str, int]
    active_filter: str
    result_line: str
    needs: list[Need] = field(default_factory=list)
    done_today: int = 0  # owner lines the viewer acted on today that no longer need them
    week: list[WeekDay] = field(default_factory=list)
    summary: str = ""  # one sentence on what is wrong across the board


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
        ask_age = 0
        if view.ask:
            asked_at = view.ask.confirmed_sent_at or view.ask.requested_at
            ask_age = dates.working_days_after(clock.local(asked_at).date(), today)
        rows[a.firedrill_id].lines.append(OwnerLine(
            assignment_id=a.public_id, owner_id=person.id, owner_name=person.display_name, owner_email=person.email,
            assigned_at=a.created_at, status=a.status, last_update_text=u.text if u else None, last_update_at=u.created_at if u else None,
            last_update_posted_for=bool(u and u.on_behalf_of_id), waiting_until=a.waiting_until,
            waiting_reason=a.waiting_reason, waiting=state.waiting, stale=state.stale, stale_days=state.stale_days,
            asked=any(k.sent for k in open_asks),
            ask_state=view.ask_state, ask=view.ask, actions=view.actions, primary=view.primary,
            ask_age_days=ask_age,
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


def _acted_on_today(s: Session, clock: Clock, viewer: Person) -> set[uuid.UUID]:
    """Owner lines the viewer emailed an ask about, or posted an update on, today."""
    start = datetime.combine(clock.today(), datetime.min.time(), clock.tz)
    asked = select(Assignment.public_id).join(UpdateRequest, UpdateRequest.assignment_id == Assignment.id).where(
        UpdateRequest.asked_by_id == viewer.id, UpdateRequest.confirmed_sent_at >= start)
    posted = select(Assignment.public_id).join(Update, Update.assignment_id == Assignment.id).where(
        Update.author_id == viewer.id, Update.created_at >= start)
    return set(s.scalars(asked.union(posted)))


def _week(rows: list[Row], today: date) -> list[WeekDay]:
    monday = today - timedelta(days=today.weekday())
    days = [monday + timedelta(days=n) for n in range(7)]
    return [WeekDay(day=d, is_today=d == today, is_past=d < today,
                    due=[r for r in rows if r.due_date == d and not r.ready_to_close]) for d in days]


def build(s: Session, clock: Clock, stale_after: int, viewer: Person, active_filter: str = "open") -> Board:
    today = clock.today()
    rows = _load(s, clock, stale_after, viewer)
    counts = {
        "open": len(rows),
        "due": sum(r.due_this_week for r in rows),
        "overdue": sum(r.overdue_days > 0 for r in rows),
    }
    if active_filter not in FILTERS:
        active_filter = "open"

    queue = needs(rows)
    waiting_on_viewer = {n.line.assignment_id for n in queue}
    done_today = len(_acted_on_today(s, clock, viewer) - waiting_on_viewer)

    # The list reads in time order: overdue first, then by due date.
    by_due = sorted(rows, key=lambda r: (r.due_date or date.max, r.created_at))
    if active_filter == "due":
        shown = [r for r in by_due if r.due_this_week]
        result_line = f"{len(shown)} {_plural(len(shown), 'firedrill')} due this week"
    elif active_filter == "overdue":
        shown = [r for r in by_due if r.overdue_days > 0]
        result_line = f"{len(shown)} overdue {_plural(len(shown), 'firedrill')}"
    else:
        shown = by_due
        result_line = f"{len(rows)} open {_plural(len(rows), 'firedrill')}"
    return Board(rows=shown, counts=counts, active_filter=active_filter, result_line=result_line,
                 needs=queue, done_today=done_today, week=_week(rows, today), summary=_summary(rows))


def _summary(rows: list[Row]) -> str:
    overdue = [r for r in rows if r.overdue_days > 0]
    quiet = sum(line.stale for r in rows for line in r.lines)
    parts = []
    if len(overdue) == 1:
        d = overdue[0].overdue_days
        parts.append(f"One firedrill is {d} {_plural(d, 'day')} overdue")
    elif overdue:
        parts.append(f"{_count_word(len(overdue))} firedrills are overdue")
    if quiet:
        verb = "has" if quiet == 1 else "have"
        phrase = f"{_count_word(quiet)} {_plural(quiet, 'owner')} {verb} gone quiet"
        parts.append(phrase if not parts else phrase[0].lower() + phrase[1:])
    if not parts:
        return "Nothing is overdue, and every owner has posted recently." if rows else ""
    return ", and ".join(parts) + "."


def _count_word(n: int) -> str:
    words = ("No", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")
    return words[n] if n < len(words) else str(n)


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


# Calendar ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WaitEnd:
    """An owner's wait that runs through this day."""

    firedrill_id: uuid.UUID
    title: str
    owner_name: str
    assignment_id: uuid.UUID


@dataclass
class CalendarDay:
    day: date
    in_month: bool
    is_today: bool
    due: list[Row] = field(default_factory=list)
    waits: list[WaitEnd] = field(default_factory=list)


@dataclass
class CalendarMonth:
    month: date  # the first of the month shown
    weeks: list[list[CalendarDay]]
    prev_month: date
    next_month: date
    is_current: bool
    due_count: int  # open firedrills due in this month
    undated: list[Row]  # open firedrills with no due date


def parse_month(value: str | None, today: date) -> date:
    """The first of a `YYYY-MM` month, or of today's month when the value is missing or bad."""
    try:
        year, month = (int(part) for part in (value or "").split("-"))
        return date(year, month, 1)
    except ValueError:
        return today.replace(day=1)


def _shift_month(first: date, delta: int) -> date:
    index = first.year * 12 + first.month - 1 + delta
    return date(index // 12, index % 12 + 1, 1)


def calendar(s: Session, clock: Clock, stale_after: int, viewer: Person, month: date) -> CalendarMonth:
    """Open firedrills by due date, and owners' waits by their last day, on a Monday-first month grid."""
    today = clock.today()
    first = month.replace(day=1)
    rows = _load(s, clock, stale_after, viewer)
    weeks = [
        [CalendarDay(day=d, in_month=d.month == first.month, is_today=d == today) for d in week]
        for week in stdcal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    ]
    cells = {cell.day: cell for week in weeks for cell in week}
    for row in sorted(rows, key=lambda r: (ROLLUP_ORDER.index(r.rollup), r.title.lower())):
        if row.due_date in cells:
            cells[row.due_date].due.append(row)
        for line in row.lines:
            if line.waiting and line.waiting_until in cells:
                cells[line.waiting_until].waits.append(
                    WaitEnd(row.public_id, row.title, line.owner_name, line.assignment_id))
    return CalendarMonth(
        month=first, weeks=weeks, prev_month=_shift_month(first, -1), next_month=_shift_month(first, 1),
        is_current=first == today.replace(day=1),
        due_count=sum(r.due_date is not None and (r.due_date.year, r.due_date.month) == (first.year, first.month)
                      for r in rows),
        undated=[r for r in rows if r.due_date is None],
    )
