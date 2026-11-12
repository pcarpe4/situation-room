"""The one module that changes firedrills, assignments, updates and asks.

Pages call these functions and never write those tables themselves. Each
function works inside the caller's transaction; the caller commits.

Lock order for anything touching an assignment: the firedrill row first, then
the assignment row. That keeps an update and an ask (or a later Close) from
interleaving.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import tickets
from .clock import Clock
from .models import (
    Assignment,
    AuditLog,
    Firedrill,
    FiredrillState,
    Person,
    Role,
    Status,
    Update,
    UpdateRequest,
    UsageEvent,
)
from .policy import Action, can

TITLE_MAX = 200
DESCRIPTION_MAX = 500
UPDATE_TEXT_MAX = 4000
OWNER_ROLES = (Role.MANAGER, Role.LEAD)


class DomainError(Exception):
    """A refused change. `code` is stable; `message` is safe to show the person."""

    def __init__(self, code: str, message: str, http_status: int = 409):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(f"{code}: {message}")


def _require(actor: Person | None, action: Action, obj: object | None = None) -> None:
    if not can(actor, action, obj):
        raise DomainError("NOT_ALLOWED", "You do not have permission to do that.", 403)


def _audit(s: Session, clock: Clock, actor: Person | None, action: str, obj_type: str, obj_id: object, **detail: object) -> None:
    s.add(AuditLog(
        actor_id=actor.id if actor else None, action=action, object_type=obj_type,
        object_id=str(obj_id), detail=detail, at=clock.now(),
    ))


# People ---------------------------------------------------------------------

def _clean_address(value: str, field: str) -> str:
    value = (value or "").strip().lower()
    if not value:
        raise DomainError("FIELD_REQUIRED", f"{field} is required.", 400)
    return value


def add_person(s: Session, clock: Clock, actor: Person | None, *, display_name: str, email: str, login_id: str, role: Role) -> Person:
    """Add someone to the roster. `actor=None` is reserved for start-up seeding."""
    if actor is not None:
        _require(actor, Action.MANAGE_ROSTER)
    name = (display_name or "").strip()
    if not name:
        raise DomainError("FIELD_REQUIRED", "Name is required.", 400)
    email = _clean_address(email, "Email")
    if "@" not in email:
        raise DomainError("FIELD_INVALID", "Email must be a mailbox address.", 400)
    login_id = _clean_address(login_id, "Sign-in value")
    if s.scalar(select(Person.id).where(Person.email == email)):
        raise DomainError("EMAIL_TAKEN", "Someone on the roster already has that email.")
    if s.scalar(select(Person.id).where(Person.login_id == login_id)):
        raise DomainError("LOGIN_TAKEN", "Someone on the roster already has that sign-in value.")
    person = Person(display_name=name, email=email, login_id=login_id, role=Role(role), active=True, created_at=clock.now())
    s.add(person)
    s.flush()
    _audit(s, clock, actor, "person.add", "person", person.id, role=person.role.value)
    return person


def deactivate_person(s: Session, clock: Clock, actor: Person, person_id: int) -> Person:
    _require(actor, Action.MANAGE_ROSTER)
    person = s.get(Person, person_id, with_for_update=True)
    if person is None:
        raise DomainError("NOT_FOUND", "No such person.", 404)
    if not person.active:
        return person
    active_work = s.scalar(
        select(func.count()).select_from(Assignment).join(Firedrill)
        .where(Assignment.owner_id == person.id, Assignment.is_active,
               Assignment.status != Status.DONE, Firedrill.state == FiredrillState.OPEN)
    )
    if active_work:
        raise DomainError(
            "PERSON_HAS_ACTIVE_WORK",
            f"{person.display_name} owns {active_work} active assignment(s). Reassign them first.",
        )
    if person.role == Role.TRIAGE:
        others = s.scalar(select(func.count()).select_from(Person).where(
            Person.role == Role.TRIAGE, Person.active.is_(True), Person.id != person.id))
        if not others:
            raise DomainError("LAST_TRIAGE", "This is the last active Triage person. Add another first.")
    person.active = False
    _audit(s, clock, actor, "person.deactivate", "person", person.id)
    return person


def ensure_first_triage(s: Session, clock: Clock, *, login_id: str, email: str, display_name: str) -> Person:
    """Run at deploy. Same mailbox means the same person (its sign-in value is corrected);
    a new mailbox is a new Triage person, and earlier rows are left alone."""
    email = _clean_address(email, "SR_FIRST_TRIAGE_EMAIL")
    login_id = _clean_address(login_id, "SR_FIRST_TRIAGE_LOGIN")
    existing = s.scalar(select(Person).where(Person.email == email))
    if existing is not None:
        if existing.login_id != login_id:
            existing.login_id = login_id
            _audit(s, clock, None, "person.correct_login", "person", existing.id)
        return existing
    return add_person(s, clock, None, display_name=display_name, email=email, login_id=login_id, role=Role.TRIAGE)


# Firedrills -------------------------------------------------------------------

def create_firedrill(
    s: Session, clock: Clock, actor: Person, *, title: str, short_description: str,
    due_date: date | None, requested_by: str | None, source: str | None,
    ticket_text: str, owner_ids: list[int],
) -> Firedrill:
    _require(actor, Action.CREATE_FIREDRILL)
    title = " ".join((title or "").split())
    short_description = " ".join((short_description or "").split())
    if not title:
        raise DomainError("FIELD_REQUIRED", "Title is required.", 400)
    if len(title) > TITLE_MAX:
        raise DomainError("FIELD_TOO_LONG", f"Title is limited to {TITLE_MAX} characters.", 400)
    if not short_description:
        raise DomainError("FIELD_REQUIRED", "Short description is required.", 400)
    if len(short_description) > DESCRIPTION_MAX:
        raise DomainError("FIELD_TOO_LONG", f"Short description is limited to {DESCRIPTION_MAX} characters.", 400)
    unique_ids = list(dict.fromkeys(owner_ids))
    if not unique_ids:
        raise DomainError("NO_OWNER", "Pick at least one owner.", 400)
    owners = s.scalars(select(Person).where(Person.id.in_(unique_ids))).all()
    if len(owners) != len(unique_ids) or any(not o.active or o.role not in OWNER_ROLES for o in owners):
        raise DomainError("OWNER_INVALID", "Owners must be active managers or leads.", 400)

    now = clock.now()
    firedrill = Firedrill(
        title=title, short_description=short_description, due_date=due_date,
        requested_by=(requested_by or "").strip() or None, source=(source or "").strip() or None,
        ticket_refs=" ".join(tickets.find_refs(ticket_text)), state=FiredrillState.OPEN,
        created_by_id=actor.id, created_at=now,
    )
    s.add(firedrill)
    s.flush()
    for owner_id in unique_ids:
        s.add(Assignment(firedrill_id=firedrill.id, owner_id=owner_id, status=Status.NEW, created_at=now))
    s.flush()
    _audit(s, clock, actor, "firedrill.create", "firedrill", firedrill.public_id, owners=len(unique_ids))
    return firedrill


# Assignments --------------------------------------------------------------------

def _lock_assignment(s: Session, public_id: uuid.UUID) -> tuple[Firedrill, Assignment]:
    """Lock the firedrill row, then the assignment row. Every assignment event takes this order."""
    firedrill_id = s.scalar(select(Assignment.firedrill_id).where(Assignment.public_id == public_id))
    if firedrill_id is None:
        raise DomainError("NOT_FOUND", "No such assignment.", 404)
    firedrill = s.scalars(
        select(Firedrill).where(Firedrill.id == firedrill_id).with_for_update().execution_options(populate_existing=True)
    ).one()
    assignment = s.scalars(
        select(Assignment).where(Assignment.public_id == public_id).with_for_update().execution_options(populate_existing=True)
    ).one()
    return firedrill, assignment


def _require_open(s: Session, firedrill: Firedrill, assignment: Assignment) -> None:
    if firedrill.state != FiredrillState.OPEN:
        raise DomainError("FIREDRILL_CLOSED", "This firedrill is closed.")
    if not assignment.is_active:
        current = s.scalars(
            select(Person).join(Assignment, Assignment.owner_id == Person.id)
            .where(Assignment.firedrill_id == firedrill.id, Assignment.is_active)
        ).all()
        names = ", ".join(p.display_name for p in current) or "nobody"
        raise DomainError("ASSIGNMENT_ENDED", f"This assignment has ended. It now belongs to {names}.")


def _open_assignment(s: Session, actor: Person, action: Action, public_id: uuid.UUID) -> tuple[Firedrill, Assignment]:
    """Lock, check permission, then check the firedrill and assignment are still open."""
    firedrill, assignment = _lock_assignment(s, public_id)
    _require(actor, action, assignment)
    _require_open(s, firedrill, assignment)
    return firedrill, assignment


@dataclass(frozen=True)
class _Event:
    """What one kind of event writes: the update row's status and text, and its effect on the assignment."""

    status: Status | None
    text: str
    audit_action: str
    apply: Callable[[Assignment], None]
    audit_detail: dict[str, object] = field(default_factory=dict)


def _record_event(
    s: Session, clock: Clock, actor: Person, public_id: uuid.UUID, *,
    form_token: str, build: Callable[[Assignment], _Event],
) -> Update:
    """The one path for an event that posts an update on an assignment.

    Order: lock (firedrill, then assignment), permission, form-token replay, open check,
    then `build` validates and describes the event. The update is written, its effect
    applied, every open ask on the assignment answered and an audit row written.
    A replayed form token returns the first update, even if the firedrill has since closed.
    """
    firedrill, assignment = _lock_assignment(s, public_id)
    _require(actor, Action.POST_UPDATE, assignment)
    if form_token:
        existing = s.scalar(select(Update).where(Update.form_token == form_token))
        if existing is not None:
            if existing.assignment_id != assignment.id:
                raise DomainError("FORM_TOKEN_REUSED", "This form was already used. Reload and try again.", 400)
            return existing
    _require_open(s, firedrill, assignment)
    event = build(assignment)

    upd = Update(
        assignment_id=assignment.id, author_id=actor.id,
        on_behalf_of_id=None if actor.id == assignment.owner_id else assignment.owner_id,
        status=event.status, text=event.text, source="form", form_token=form_token or None, created_at=clock.now(),
    )
    s.add(upd)
    s.flush()
    event.apply(assignment)
    s.execute(
        update(UpdateRequest)
        .where(UpdateRequest.assignment_id == assignment.id, UpdateRequest.is_open)
        .values(answered_by_update_id=upd.id)
    )
    _audit(s, clock, actor, event.audit_action, "assignment", assignment.public_id,
           on_behalf=upd.on_behalf_of_id is not None, **event.audit_detail)
    return upd


def post_update(
    s: Session, clock: Clock, actor: Person, assignment_public_id: uuid.UUID, *,
    status: Status | str, text: str, form_token: str,
) -> Update:
    """Post an update with a status. A repeated Send returns the first update."""

    def build(assignment: Assignment) -> _Event:
        try:
            new_status = Status(status)
        except ValueError:
            raise DomainError("STATUS_REQUIRED", "Pick a status.", 400) from None
        if new_status == Status.NEW:
            raise DomainError("STATUS_REQUIRED", "Pick a status.", 400)
        body = (text or "").strip()
        if len(body) > UPDATE_TEXT_MAX:
            raise DomainError("FIELD_TOO_LONG", f"Updates are limited to {UPDATE_TEXT_MAX} characters.", 400)

        def apply(a: Assignment) -> None:
            a.status = new_status
            if new_status == Status.DONE:
                a.waiting_until = None
                a.waiting_reason = None

        return _Event(new_status, body, "update.post", apply, {"status": new_status.value})

    return _record_event(s, clock, actor, assignment_public_id, form_token=form_token, build=build)


def set_wait(
    s: Session, clock: Clock, actor: Person, assignment_public_id: uuid.UUID, *,
    until: date, reason: str, form_token: str = "",
) -> Update:
    """Pause the stale clock through `until`. Recorded as a status-less update whose text is
    the reason, so it answers open asks like any other update."""

    def build(assignment: Assignment) -> _Event:
        if assignment.status == Status.DONE:
            raise DomainError("ASSIGNMENT_DONE", "This assignment is done, so it cannot wait.")
        why = " ".join((reason or "").split())
        if not why:
            raise DomainError("FIELD_REQUIRED", "Say what you are waiting for.", 400)
        if until <= clock.today():
            raise DomainError("FIELD_INVALID", "The waiting date must be in the future.", 400)

        def apply(a: Assignment) -> None:
            a.waiting_until = until
            a.waiting_reason = why

        return _Event(None, why, "wait.set", apply, {"until": until.isoformat()})

    return _record_event(s, clock, actor, assignment_public_id, form_token=form_token, build=build)


def request_update(s: Session, clock: Clock, actor: Person, assignment_public_id: uuid.UUID) -> UpdateRequest:
    """Record an ask. Always creates a new one, even while another is open."""
    _, assignment = _open_assignment(s, actor, Action.REQUEST_UPDATE, assignment_public_id)
    if assignment.status == Status.DONE:
        raise DomainError("ASSIGNMENT_DONE", "This assignment is done, so there is nothing to ask.")
    ask = UpdateRequest(assignment_id=assignment.id, asked_by_id=actor.id, origin="manual", requested_at=clock.now())
    s.add(ask)
    s.flush()
    _audit(s, clock, actor, "ask.create", "assignment", assignment.public_id, ask_id=ask.id)
    return ask


def _lock_ask(s: Session, actor: Person, ask_id: int) -> UpdateRequest:
    _require(actor, Action.REQUEST_UPDATE)
    ask = s.get(UpdateRequest, ask_id, with_for_update=True)
    if ask is None:
        raise DomainError("NOT_FOUND", "No such ask.", 404)
    return ask


def confirm_ask_sent(s: Session, clock: Clock, actor: Person, ask_id: int) -> UpdateRequest:
    ask = _lock_ask(s, actor, ask_id)
    if ask.confirmed_sent_at is None and ask.is_open:
        ask.confirmed_sent_at = clock.now()
        _audit(s, clock, actor, "ask.confirm_sent", "update_request", ask.id)
    return ask


def cancel_ask(s: Session, clock: Clock, actor: Person, ask_id: int, reason: str = "not_sent") -> UpdateRequest:
    ask = _lock_ask(s, actor, ask_id)
    if ask.answered_by_update_id is not None:
        raise DomainError("ASK_ANSWERED", "That ask was already answered.")
    if ask.is_open:
        ask.cancelled_at = clock.now()
        ask.cancel_reason = reason
        _audit(s, clock, actor, "ask.cancel", "update_request", ask.id, reason=reason)
    return ask


def record_board_view(s: Session, clock: Clock, person: Person) -> None:
    s.execute(
        insert(UsageEvent).values(person_id=person.id, day=clock.today(), kind="board_view")
        .on_conflict_do_nothing()
    )
