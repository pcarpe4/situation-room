"""Page handlers. They check permission through `policy`, change state only through
`domain`, and commit. They hold no business rules of their own."""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .. import board as board_mod
from .. import domain, mailto, passwords
from ..models import Assignment, Firedrill, FiredrillState, Person, Role, Status, Update, UpdateRequest
from ..policy import Action, can
from .render import FOCUS_COOKIE, THEME_COOKIE, THEMES, render
from .security import KNOWN_COOKIE, Refused, current_person, get_db, safe_next, start_session

router = APIRouter()


def _cfg(request: Request):
    return request.app.state.cfg


def _clock(request: Request):
    return request.app.state.clock


def _require(person: Person, action: Action, obj: object | None = None) -> None:
    if not can(person, action, obj):
        raise Refused("NOT_ALLOWED", "You do not have permission to open this page.")


def _see_other(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


# Health -------------------------------------------------------------------------

@router.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


@router.get("/healthz/db", response_class=PlainTextResponse)
def healthz_db(db: Session = Depends(get_db)) -> str:
    db.execute(text("select 1"))
    return "ok"


# Sign-in and profiles ---------------------------------------------------------------

YEAR = 400 * 24 * 3600


def _signed_in(request: Request, person: Person, next_path: str) -> RedirectResponse:
    start_session(request, person)
    response = _see_other(safe_next(next_path))
    response.set_cookie(KNOWN_COOKIE, "1", max_age=YEAR, httponly=True, samesite="lax",
                        secure=_cfg(request).public_url.startswith("https://"))
    return response


def _require_profiles(request: Request) -> None:
    if not _cfg(request).has_profiles:
        raise Refused("SIGNIN_MODE", "Profiles are switched off here. Sign in through your organisation.", 404)


def _signin_page(request: Request, db: Session, next_path: str, *, status_code: int = 200,
                 email: str = "", error: str | None = None):
    people = []
    if _cfg(request).is_dev_signin:
        people = db.scalars(select(Person).where(Person.active.is_(True)).order_by(Person.role, Person.display_name)).all()
    return render(request, "signin.html", status_code=status_code, people=people, next=safe_next(next_path),
                  email=email, error=error)


@router.get("/signin")
def signin_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    return _signin_page(request, db, next)


@router.post("/signin")
def signin(request: Request, email: str = Form(""), password: str = Form(""), next: str = Form("/"),
           db: Session = Depends(get_db)):
    _require_profiles(request)
    throttle = request.app.state.signin_throttle
    if throttle.blocked(email):
        return _signin_page(request, db, next, status_code=429, email=email,
                            error="Too many tries for that email. Wait ten minutes, or ask your Triage person for a password link.")
    person = domain.authenticate(db, email, password)
    if person is None:
        throttle.failed(email)
        return _signin_page(request, db, next, status_code=401, email=email,
                            error="That email and password do not match a profile.")
    throttle.succeeded(email)
    return _signed_in(request, person, next)


@router.post("/signin/dev")
def signin_dev(request: Request, person_id: int = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    if not _cfg(request).is_dev_signin:
        raise Refused("SIGNIN_MODE", "Development sign-in is switched off.", 404)
    person = db.get(Person, person_id)
    if person is None or not person.active:
        raise Refused("NO_ACCESS", "That person is not on the active roster.")
    return _signed_in(request, person, next)


def _welcome_page(request: Request, db: Session, next_path: str, *, status_code: int = 200,
                  form: dict | None = None, error: str | None = None):
    return render(request, "welcome.html", status_code=status_code, next=safe_next(next_path), form=form or {},
                  error=error, first=not domain.has_admin(db), min_length=passwords.MIN_LENGTH)


@router.get("/welcome")
def welcome_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    _require_profiles(request)
    return _welcome_page(request, db, next)


@router.post("/welcome")
def welcome(request: Request, display_name: str = Form(""), email: str = Form(""), password: str = Form(""),
            password_again: str = Form(""), next: str = Form("/"), db: Session = Depends(get_db)):
    _require_profiles(request)
    values = {"display_name": display_name, "email": email}
    if password != password_again:
        return _welcome_page(request, db, next, status_code=400, form=values, error="The two passwords do not match.")
    try:
        person = domain.register_profile(db, _clock(request), display_name=display_name, email=email, password=password)
    except domain.DomainError as exc:
        db.rollback()
        return _welcome_page(request, db, next, status_code=exc.http_status, form=values, error=exc.message)
    db.commit()
    return _signed_in(request, person, next)


def _person_for_link(request: Request, db: Session, token: str) -> Person:
    """The person a password link was made for, while the link is still good."""
    _require_profiles(request)
    found = passwords.read_link_token(_cfg(request).secret_key, token)
    person = db.get(Person, found[0]) if found else None
    if person is None or not person.active or passwords.fingerprint(person.password_hash) != found[1]:
        raise Refused("LINK_SPENT", "This password link has been used or is too old. Ask your Triage person for a new one.", 410)
    return person


@router.get("/password/{token}")
def password_page(request: Request, token: str, db: Session = Depends(get_db)):
    person = _person_for_link(request, db, token)
    return render(request, "password.html", person=person, token=token, error=None, min_length=passwords.MIN_LENGTH)


@router.post("/password/{token}")
def password_set(request: Request, token: str, password: str = Form(""), password_again: str = Form(""),
                 db: Session = Depends(get_db)):
    person = _person_for_link(request, db, token)
    error = None
    if password != password_again:
        error = "The two passwords do not match."
    else:
        try:
            person = domain.set_password(db, _clock(request), person.id, password)
        except domain.DomainError as exc:
            if exc.http_status != 400:
                raise
            db.rollback()
            error = exc.message
    if error:
        return render(request, "password.html", status_code=400, person=person, token=token, error=error,
                      min_length=passwords.MIN_LENGTH)
    db.commit()
    return _signed_in(request, person, "/")


@router.post("/signout")
def signout(request: Request):
    request.session.clear()
    return _see_other("/signin")


@router.post("/theme")
def set_theme(request: Request, theme: str = Form("system"), back: str = Form("/")):
    """Light, dark or follow the device. Kept in its own cookie so it outlives sign-out."""
    response = _see_other(safe_next(back, "/"))
    if theme in THEMES and theme != "system":
        response.set_cookie(THEME_COOKIE, theme, max_age=400 * 24 * 3600, httponly=True, samesite="lax",
                            secure=_cfg(request).public_url.startswith("https://"))
    else:
        response.delete_cookie(THEME_COOKIE)
    return response


@router.post("/focus")
def set_focus(request: Request, focus: str = Form("off"), back: str = Form("/board")):
    """Focus mode tucks the board's list away so only what needs you shows. Kept like the theme."""
    response = _see_other(safe_next(back, "/board"))
    if focus == "on":
        response.set_cookie(FOCUS_COOKIE, "on", max_age=400 * 24 * 3600, httponly=True, samesite="lax",
                            secure=_cfg(request).public_url.startswith("https://"))
    else:
        response.delete_cookie(FOCUS_COOKIE)
    return response


# Board ------------------------------------------------------------------------------

@router.get("/")
def home(me: Person = Depends(current_person)):
    return _see_other("/mine" if me.role == Role.MANAGER else "/board")


@router.get("/board")
def board_page(request: Request, filter: str = "open", me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.VIEW_BOARD)
    view = board_mod.build(db, _clock(request), _cfg(request).stale_after, me, filter)
    domain.record_board_view(db, _clock(request), me)
    db.commit()
    return render(request, "board.html", board=view, tab="board", ask_link=_ask_link(request, me))


@router.get("/calendar")
def calendar_page(request: Request, month: str | None = None, me: Person = Depends(current_person),
                  db: Session = Depends(get_db)):
    _require(me, Action.VIEW_BOARD)
    clock = _clock(request)
    view = board_mod.calendar(db, clock, _cfg(request).stale_after, me, board_mod.parse_month(month, clock.today()))
    return render(request, "calendar.html", cal=view, tab="calendar")


@router.get("/mine")
def mine_page(request: Request, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    rows = board_mod.mine(db, _clock(request), _cfg(request).stale_after, me)
    return render(request, "mine.html", rows=rows, tab="mine", ask_link=_ask_link(request, me))


def _ask_link(request: Request, me: Person):
    public_url = _cfg(request).public_url

    def build(row: board_mod.Row, line: board_mod.OwnerLine) -> str:
        return mailto.ask_mailto(
            public_url=public_url, to_email=line.owner_email, title=row.title, due_date=row.due_date,
            assignment_public_id=line.assignment_id, asker_name=me.display_name,
        )
    return build


# Firedrills ---------------------------------------------------------------------------

def _owner_choices(db: Session) -> list[Person]:
    return list(db.scalars(
        select(Person).where(Person.active.is_(True), Person.role.in_(domain.OWNER_ROLES)).order_by(Person.display_name)
    ))


@router.get("/firedrills/new")
def new_firedrill_page(request: Request, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.CREATE_FIREDRILL)
    return render(request, "new_firedrill.html", owners=_owner_choices(db), form={}, chosen=set(), error=None, tab="new")


@router.post("/firedrills/new")
async def create_firedrill(request: Request, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.CREATE_FIREDRILL)
    form = await request.form()
    values = {k: str(form.get(k) or "") for k in ("title", "short_description", "due_date", "requested_by", "source", "ticket_text")}
    chosen = {int(v) for v in form.getlist("owner_ids") if str(v).isdigit()}
    try:
        due = date.fromisoformat(values["due_date"]) if values["due_date"] else None
        firedrill = domain.create_firedrill(
            db, _clock(request), me, title=values["title"], short_description=values["short_description"],
            due_date=due, requested_by=values["requested_by"], source=values["source"],
            ticket_text=values["ticket_text"], owner_ids=sorted(chosen),
        )
    except ValueError:
        db.rollback()
        return render(request, "new_firedrill.html", status_code=400, owners=_owner_choices(db),
                      form=values, chosen=chosen, error="Due date is not a valid date.", tab="new")
    except domain.DomainError as exc:
        db.rollback()
        return render(request, "new_firedrill.html", status_code=exc.http_status, owners=_owner_choices(db),
                      form=values, chosen=chosen, error=exc.message, tab="new")
    db.commit()
    return _see_other(f"/f/{firedrill.public_id}")


@router.get("/f/{public_id}")
def firedrill_page(request: Request, public_id: uuid.UUID, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.VIEW_FIREDRILL)
    firedrill = db.scalar(select(Firedrill).where(Firedrill.public_id == public_id))
    if firedrill is None:
        raise Refused("NOT_FOUND", "No such firedrill.", 404)
    row = board_mod.one(db, _clock(request), _cfg(request).stale_after, me, public_id)
    history = db.execute(
        select(Update, Person, Assignment)
        .join(Assignment, Assignment.id == Update.assignment_id)
        .join(Person, Person.id == Assignment.owner_id)
        .where(Assignment.firedrill_id == firedrill.id)
        .order_by(Update.id.desc())
    ).all()
    authors = {p.id: p for p in db.scalars(select(Person).where(Person.id.in_({u.author_id for u, _, _ in history})))}
    return render(request, "firedrill.html", firedrill=firedrill, row=row, history=history, authors=authors,
                  ask_link=_ask_link(request, me))


# Updates and asks ---------------------------------------------------------------------

def _assignment_context(db: Session, public_id: uuid.UUID):
    found = db.execute(
        select(Assignment, Firedrill, Person)
        .join(Firedrill, Firedrill.id == Assignment.firedrill_id)
        .join(Person, Person.id == Assignment.owner_id)
        .where(Assignment.public_id == public_id)
    ).one_or_none()
    if found is None:
        raise Refused("NOT_FOUND", "No such assignment. Check the link.", 404)
    return found


def _render_update_page(request: Request, db: Session, me: Person, public_id: uuid.UUID, *,
                        status_code: int = 200, **form: object):
    """The update page, with its wait form. Errors re-render it through here, so what was typed
    is kept and the page still shows the last update and who asked."""
    assignment, firedrill, owner = _assignment_context(db, public_id)
    if not can(me, Action.POST_UPDATE, assignment):
        raise Refused("NOT_OWNER", f"This assignment belongs to {owner.display_name}.")
    last = db.scalar(select(Update).where(Update.assignment_id == assignment.id).order_by(Update.id.desc()).limit(1))
    open_ask = db.scalar(
        select(UpdateRequest).where(UpdateRequest.assignment_id == assignment.id, UpdateRequest.is_open)
        .order_by(UpdateRequest.id.desc()).limit(1)
    )
    asker = db.get(Person, open_ask.asked_by_id) if open_ask else None
    context = dict(
        chosen=None, form_token=uuid.uuid4().hex, error=None, text_value="",
        wait_token=uuid.uuid4().hex, wait_error=None, wait_until="", wait_reason="",
    )
    context.update(form)
    return render(
        request, "update.html", status_code=status_code, assignment=assignment, firedrill=firedrill, owner=owner,
        from_link=context["chosen"] is not None, last=last, open_ask=open_ask, asker=asker,
        closed=firedrill.state != FiredrillState.OPEN, ended=not assignment.is_active,
        earliest_wait=domain.earliest_wait_date(_clock(request)), wait_reason_max=domain.WAIT_REASON_MAX, **context,
    )


@router.get("/a/{public_id}/update")
def update_page(request: Request, public_id: uuid.UUID, status: str | None = None,
                me: Person = Depends(current_person), db: Session = Depends(get_db)):
    chosen = status if status in {s.value for s in mailto_statuses()} else None
    return _render_update_page(request, db, me, public_id, chosen=chosen)


def mailto_statuses() -> list[Status]:
    return [s for s, _ in mailto.STATUS_LINKS]


@router.post("/a/{public_id}/update")
def post_update(request: Request, public_id: uuid.UUID, status: str = Form(""), text: str = Form(""),
                form_token: str = Form(""), me: Person = Depends(current_person), db: Session = Depends(get_db)):
    clock = _clock(request)
    try:
        upd = domain.post_update(db, clock, me, public_id, status=status, text=text, form_token=form_token)
    except domain.DomainError as exc:
        # Re-raise before any rollback: a rollback expires `me`, which the error page still reads.
        if exc.code not in ("STATUS_REQUIRED", "FIELD_TOO_LONG"):
            raise
        db.rollback()
        return _render_update_page(request, db, me, public_id, status_code=exc.http_status,
                                   form_token=form_token, error=exc.message, text_value=text)
    db.commit()
    answered = db.scalars(select(UpdateRequest).where(UpdateRequest.answered_by_update_id == upd.id)).all()
    assignment, firedrill, owner = _assignment_context(db, public_id)
    others = [
        (row, line) for row in board_mod.mine(db, clock, _cfg(request).stale_after, me, owner.id)
        for line in row.lines
        if line.owner_id == owner.id and line.assignment_id != public_id and (line.stale or line.asked)
    ]
    return render(request, "update_sent.html", update=upd, firedrill=firedrill, owner=owner,
                  answered=answered, others=others)


@router.post("/a/{public_id}/wait")
def set_wait(request: Request, public_id: uuid.UUID, until: str = Form(""), reason: str = Form(""),
             form_token: str = Form(""), me: Person = Depends(current_person), db: Session = Depends(get_db)):
    def again(status_code: int, message: str):
        return _render_update_page(request, db, me, public_id, status_code=status_code, wait_error=message,
                                   wait_token=form_token, wait_until=until, wait_reason=reason)

    try:
        until_date = date.fromisoformat(until)
    except ValueError:
        return again(400, "Pick a date.")
    try:
        domain.set_wait(db, _clock(request), me, public_id, until=until_date, reason=reason, form_token=form_token)
    except domain.DomainError as exc:
        if exc.code not in ("FIELD_REQUIRED", "FIELD_INVALID", "FIELD_TOO_LONG", "ASSIGNMENT_DONE"):
            raise
        db.rollback()
        return again(exc.http_status, exc.message)
    db.commit()
    _, firedrill, _ = _assignment_context(db, public_id)
    return _see_other(f"/f/{firedrill.public_id}#a-{public_id}")


@router.post("/a/{public_id}/ask")
def ask(request: Request, public_id: uuid.UUID, back: str = Form("/board"),
        me: Person = Depends(current_person), db: Session = Depends(get_db)):
    domain.request_update(db, _clock(request), me, public_id)
    db.commit()
    return _see_other(f"{safe_next(back, '/board')}#a-{public_id}")


@router.post("/asks/{ask_id}/sent")
def ask_sent(request: Request, ask_id: int, back: str = Form("/board"),
             me: Person = Depends(current_person), db: Session = Depends(get_db)):
    domain.confirm_ask_sent(db, _clock(request), me, ask_id)
    db.commit()
    return _see_other(safe_next(back, "/board"))


@router.post("/asks/{ask_id}/cancel")
def ask_cancel(request: Request, ask_id: int, back: str = Form("/board"),
               me: Person = Depends(current_person), db: Session = Depends(get_db)):
    domain.cancel_ask(db, _clock(request), me, ask_id)
    db.commit()
    return _see_other(safe_next(back, "/board"))


# Roster -----------------------------------------------------------------------------

def _roster(db: Session) -> list[Person]:
    return list(db.scalars(select(Person).order_by(Person.active.desc(), Person.display_name)))


def _roster_page(request: Request, db: Session, *, status_code: int = 200, form: dict | None = None,
                 error: str | None = None):
    return render(request, "roster.html", status_code=status_code, people=_roster(db), roles=list(Role),
                  form=form or {}, error=error, tab="roster")


def _person_page(request: Request, db: Session, me: Person, person_id: int, *, status_code: int = 200,
                 form: dict | None = None, error: str | None = None, link: str | None = None):
    person = db.get(Person, person_id)
    if person is None:
        raise Refused("NOT_FOUND", "No such person.", 404)
    link_mail = link and mailto.password_link_mailto(
        to_email=person.email, to_name=person.display_name, link=link, sender_name=me.display_name,
        days=passwords.LINK_MAX_AGE // 86400)
    return render(request, "person.html", status_code=status_code, person=person, roles=list(Role),
                  form=form or {}, error=error, link=link, link_mail=link_mail,
                  link_days=passwords.LINK_MAX_AGE // 86400, tab="roster")


@router.get("/roster")
def roster_page(request: Request, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.MANAGE_ROSTER)
    return _roster_page(request, db)


@router.post("/roster")
def roster_add(request: Request, display_name: str = Form(""), email: str = Form(""), login_id: str = Form(""),
               role: str = Form("manager"), me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.MANAGE_ROSTER)
    values = {"display_name": display_name, "email": email, "login_id": login_id, "role": role}
    try:
        person = domain.add_person(db, _clock(request), me, display_name=display_name, email=email,
                                   login_id=login_id, role=Role(role) if role in {r.value for r in Role} else Role.MANAGER)
    except domain.DomainError as exc:
        db.rollback()
        return _roster_page(request, db, status_code=exc.http_status, form=values, error=exc.message)
    db.commit()
    # With profiles, the next step is on their page: a password link to send them.
    return _see_other(f"/roster/{person.id}" if _cfg(request).has_profiles else "/roster")


@router.get("/roster/{person_id}")
def person_page(request: Request, person_id: int, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.MANAGE_ROSTER)
    return _person_page(request, db, me, person_id)


@router.post("/roster/{person_id}")
def person_save(request: Request, person_id: int, display_name: str = Form(""), email: str = Form(""),
                role: str = Form(""), me: Person = Depends(current_person), db: Session = Depends(get_db)):
    values = {"display_name": display_name, "email": email, "role": role}
    return _person_change(
        request, db, me, person_id, values,
        lambda: domain.update_person(db, _clock(request), me, person_id, display_name=display_name, email=email, role=role),
    )


@router.post("/roster/{person_id}/deactivate")
def roster_deactivate(request: Request, person_id: int, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    return _person_change(request, db, me, person_id, {},
                          lambda: domain.deactivate_person(db, _clock(request), me, person_id))


@router.post("/roster/{person_id}/reactivate")
def roster_reactivate(request: Request, person_id: int, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    return _person_change(request, db, me, person_id, {},
                          lambda: domain.reactivate_person(db, _clock(request), me, person_id))


@router.post("/roster/{person_id}/remove")
def roster_remove(request: Request, person_id: int, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    return _person_change(request, db, me, person_id, {},
                          lambda: domain.remove_person(db, _clock(request), me, person_id), then="/roster")


def _person_change(request: Request, db: Session, me: Person, person_id: int, values: dict, change, then: str | None = None):
    """Run one roster change. A refusal that is about the data comes back on the person's page."""
    try:
        change()
    except domain.DomainError as exc:
        # Re-raise before any rollback: a rollback expires `me`, which the error page still reads.
        if exc.http_status in (403, 404):
            raise
        db.rollback()
        return _person_page(request, db, me, person_id, status_code=exc.http_status, form=values, error=exc.message)
    db.commit()
    return _see_other(then or f"/roster/{person_id}")


@router.post("/roster/{person_id}/password-link")
def roster_password_link(request: Request, person_id: int, me: Person = Depends(current_person),
                         db: Session = Depends(get_db)):
    """Make a one-time link the person uses to choose a password. Triage passes it on."""
    _require(me, Action.MANAGE_ROSTER)
    _require_profiles(request)
    person = db.get(Person, person_id)
    if person is None:
        raise Refused("NOT_FOUND", "No such person.", 404)
    if not person.active:
        return _person_page(request, db, me, person_id, status_code=409,
                            error=f"{person.display_name} is inactive. Reactivate them first.")
    cfg = _cfg(request)
    link = f"{cfg.public_url}/password/{passwords.link_token(cfg.secret_key, person.id, person.password_hash)}"
    domain.record_password_link(db, _clock(request), me, person)
    db.commit()
    return _person_page(request, db, me, person_id, link=link)
