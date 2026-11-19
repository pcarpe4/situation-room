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
from datetime import date, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from . import passwords, tickets
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
WAIT_REASON_MAX = 300
NAME_MAX = 200
ADDRESS_MAX = 320
_PROFILE_LOCK = 7_411_001
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


def _clean_name(display_name: str) -> str:
    name = " ".join((display_name or "").split())
    if not name:
        raise DomainError("FIELD_REQUIRED", "Name is required.", 400)
    if len(name) > NAME_MAX:
        raise DomainError("FIELD_TOO_LONG", f"Name is too long. Keep it under {NAME_MAX} characters.", 400)
    return name


def _clean_email(email: str) -> str:
    email = _clean_address(email, "Email")
    if "@" not in email or len(email) > ADDRESS_MAX:
        raise DomainError("FIELD_INVALID", "Email must be a mailbox address.", 400)
    return email


def _check_password(password: str) -> None:
    if len(password or "") < passwords.MIN_LENGTH:
        raise DomainError("FIELD_INVALID", f"Password must be at least {passwords.MIN_LENGTH} characters.", 400)
    if len(password) > passwords.MAX_LENGTH:
        raise DomainError("FIELD_TOO_LONG", f"Password must be under {passwords.MAX_LENGTH} characters.", 400)


def _new_person(s: Session, clock: Clock, actor: Person | None, *, name: str, email: str, login_id: str,
                role: Role, password_hash: str | None = None, taken: str) -> Person:
    if s.scalar(select(Person.id).where(Person.email == email)):
        raise DomainError("EMAIL_TAKEN", taken)
    if s.scalar(select(Person.id).where(Person.login_id == login_id)):
        raise DomainError("LOGIN_TAKEN", "Someone on the roster already has that sign-in value.")
    person = Person(display_name=name, email=email, login_id=login_id, role=Role(role), active=True,
                    password_hash=password_hash, created_at=clock.now())
    s.add(person)
    s.flush()
    return person


def add_person(s: Session, clock: Clock, actor: Person | None, *, display_name: str, email: str,
               login_id: str | None = None, role: Role) -> Person:
    """Add someone to the roster. `actor=None` is reserved for start-up set-up.
    The sign-in value is the email unless a directory presents something else."""
    if actor is not None:
        _require(actor, Action.MANAGE_ROSTER)
    name = _clean_name(display_name)
    email = _clean_email(email)
    login_id = _clean_address(login_id, "Sign-in value") if (login_id or "").strip() else email
    person = _new_person(s, clock, actor, name=name, email=email, login_id=login_id, role=role,
                         taken="Someone on the roster already has that email.")
    _audit(s, clock, actor, "person.add", "person", person.id, role=person.role.value)
    return person


def has_admin(s: Session) -> bool:
    """Whether anyone can manage the roster. The first profile made while nobody can becomes Triage."""
    return s.scalar(select(Person.id).where(Person.role == Role.TRIAGE, Person.active.is_(True)).limit(1)) is not None


def register_profile(s: Session, clock: Clock, *, display_name: str, email: str, password: str) -> Person:
    """Someone opening the app for the first time makes their own profile.

    They join as a Manager, the role that can do the least; Triage raises it from the
    roster. The exception is the first profile made while there is no Triage person:
    that one becomes Triage, so a new install has someone who can manage the rest.
    """
    name = _clean_name(display_name)
    email = _clean_email(email)
    _check_password(password)
    # One sign-up at a time, so two people arriving together cannot both be "the first".
    s.execute(sql_text("select pg_advisory_xact_lock(:key)"), {"key": _PROFILE_LOCK})
    role = Role.MANAGER if has_admin(s) else Role.TRIAGE
    person = _new_person(
        s, clock, None, name=name, email=email, login_id=email, role=role,
        password_hash=passwords.hash_password(password),
        taken="There is already a profile for that email. Sign in, or ask your Triage person for a password link.",
    )
    _audit(s, clock, person, "person.register", "person", person.id, role=person.role.value)
    return person


def authenticate(s: Session, email: str, password: str) -> Person | None:
    """The active person with this email and password, or None. Changes nothing."""
    person = s.scalar(select(Person).where(Person.email == (email or "").strip().lower()))
    matches = passwords.verify_password(password or "", person.password_hash if person else None)
    return person if matches and person is not None and person.active else None


def set_password(s: Session, clock: Clock, person_id: int, password: str) -> Person:
    """A person picks a new password from a password link. The link is the permission."""
    _check_password(password)
    person = s.get(Person, person_id, with_for_update=True)
    if person is None or not person.active:
        raise DomainError("NOT_FOUND", "No such person.", 404)
    person.password_hash = passwords.hash_password(password)
    _audit(s, clock, person, "person.set_password", "person", person.id)
    return person


def record_password_link(s: Session, clock: Clock, actor: Person, person: Person) -> None:
    """Triage made a password link for someone. The link itself is never stored."""
    _require(actor, Action.MANAGE_ROSTER)
    _audit(s, clock, actor, "person.password_link", "person", person.id)


def _roster_target(s: Session, actor: Person, person_id: int) -> Person:
    _require(actor, Action.MANAGE_ROSTER)
    person = s.get(Person, person_id, with_for_update=True)
    if person is None:
        raise DomainError("NOT_FOUND", "No such person.", 404)
    return person


def _require_another_triage(s: Session, person: Person) -> None:
    others = s.scalar(select(func.count()).select_from(Person).where(
        Person.role == Role.TRIAGE, Person.active.is_(True), Person.id != person.id))
    if not others:
        raise DomainError("LAST_TRIAGE", "This is the last active Triage person. Add another first.")


def update_person(s: Session, clock: Clock, actor: Person, person_id: int, *, display_name: str, email: str,
                  role: Role | str) -> Person:
    """Triage corrects a name or mailbox, or changes a role."""
    person = _roster_target(s, actor, person_id)
    name = _clean_name(display_name)
    email = _clean_email(email)
    try:
        role = Role(role)
    except ValueError:
        raise DomainError("FIELD_INVALID", "Pick a role.", 400) from None
    changed = []
    if name != person.display_name:
        person.display_name = name
        changed.append("display_name")
    if email != person.email:
        if s.scalar(select(Person.id).where(Person.email == email, Person.id != person.id)):
            raise DomainError("EMAIL_TAKEN", "Someone on the roster already has that email.")
        if person.login_id == person.email:
            # A local profile signs in with its email, so the two move together.
            if s.scalar(select(Person.id).where(Person.login_id == email, Person.id != person.id)):
                raise DomainError("LOGIN_TAKEN", "Someone on the roster already has that sign-in value.")
            person.login_id = email
        person.email = email
        changed.append("email")
    if role != person.role:
        if person.role == Role.TRIAGE and person.active:
            _require_another_triage(s, person)
        person.role = role
        changed.append("role")
    if changed:
        _audit(s, clock, actor, "person.update", "person", person.id, fields=changed, role=person.role.value)
    return person


def deactivate_person(s: Session, clock: Clock, actor: Person, person_id: int) -> Person:
    person = _roster_target(s, actor, person_id)
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
        _require_another_triage(s, person)
    person.active = False
    _audit(s, clock, actor, "person.deactivate", "person", person.id)
    return person


def reactivate_person(s: Session, clock: Clock, actor: Person, person_id: int) -> Person:
    person = _roster_target(s, actor, person_id)
    if not person.active:
        person.active = True
        _audit(s, clock, actor, "person.reactivate", "person", person.id)
    return person


def remove_person(s: Session, clock: Clock, actor: Person, person_id: int) -> None:
    """Delete a profile outright. Only for one with no history (a mistaken or unwanted sign-up);
    anyone who has owned, written or asked for anything is deactivated instead."""
    person = _roster_target(s, actor, person_id)
    if person.id == actor.id:
        raise DomainError("IS_YOU", "You cannot remove your own profile.")
    has_history = any(
        s.scalar(select(column).where(column == person.id).limit(1)) is not None
        for column in (Firedrill.created_by_id, Assignment.owner_id, Update.author_id,
                       Update.on_behalf_of_id, UpdateRequest.asked_by_id)
    )
    if has_history:
        raise DomainError("PERSON_HAS_HISTORY", f"{person.display_name} has history on the board. Deactivate them instead.")
    if person.role == Role.TRIAGE and person.active:
        _require_another_triage(s, person)
    s.execute(delete(UsageEvent).where(UsageEvent.person_id == person.id))
    s.execute(update(AuditLog).where(AuditLog.actor_id == person.id).values(actor_id=None))
    s.delete(person)
    s.flush()
    _audit(s, clock, actor, "person.remove", "person", person_id)


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


def earliest_wait_date(clock: Clock) -> date:
    """A wait runs through its date, so the earliest useful one is tomorrow."""
    return clock.today() + timedelta(days=1)


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
        if len(why) > WAIT_REASON_MAX:
            raise DomainError("FIELD_TOO_LONG", f"Keep the reason under {WAIT_REASON_MAX} characters.", 400)
        if until < earliest_wait_date(clock):
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
