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
from .. import domain, mailto
from ..models import Assignment, Firedrill, FiredrillState, Person, Role, Status, Update, UpdateRequest
from ..policy import Action, can
from .render import THEME_COOKIE, THEMES, render
from .security import Refused, current_person, get_db, safe_next

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


# Sign-in --------------------------------------------------------------------------

@router.get("/signin")
def signin_page(request: Request, next: str = "/", db: Session = Depends(get_db)):
    people = db.scalars(select(Person).where(Person.active.is_(True)).order_by(Person.role, Person.display_name)).all()
    return render(request, "signin.html", people=people, next=safe_next(next))


@router.post("/signin/dev")
def signin_dev(request: Request, person_id: int = Form(...), next: str = Form("/"), db: Session = Depends(get_db)):
    if not _cfg(request).is_dev_signin:
        raise Refused("SIGNIN_MODE", "Development sign-in is switched off.", 404)
    person = db.get(Person, person_id)
    if person is None or not person.active:
        raise Refused("NO_ACCESS", "That person is not on the active roster.")
    request.session.clear()
    request.session["person_id"] = person.id
    return _see_other(safe_next(next))


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

@router.get("/roster")
def roster_page(request: Request, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.MANAGE_ROSTER)
    return render(request, "roster.html", people=_roster(db), roles=list(Role), form={}, error=None, tab="roster")


def _roster(db: Session) -> list[Person]:
    return list(db.scalars(select(Person).order_by(Person.active.desc(), Person.display_name)))


@router.post("/roster")
def roster_add(request: Request, display_name: str = Form(""), email: str = Form(""), login_id: str = Form(""),
               role: str = Form("manager"), me: Person = Depends(current_person), db: Session = Depends(get_db)):
    _require(me, Action.MANAGE_ROSTER)
    values = {"display_name": display_name, "email": email, "login_id": login_id, "role": role}
    try:
        domain.add_person(db, _clock(request), me, display_name=display_name, email=email,
                          login_id=login_id, role=Role(role) if role in {r.value for r in Role} else Role.MANAGER)
    except domain.DomainError as exc:
        db.rollback()
        return render(request, "roster.html", status_code=exc.http_status, people=_roster(db), roles=list(Role),
                      form=values, error=exc.message, tab="roster")
    db.commit()
    return _see_other("/roster")


@router.post("/roster/{person_id}/deactivate")
def roster_deactivate(request: Request, person_id: int, me: Person = Depends(current_person), db: Session = Depends(get_db)):
    try:
        domain.deactivate_person(db, _clock(request), me, person_id)
    except domain.DomainError as exc:
        if exc.http_status == 403:
            raise
        db.rollback()
        return render(request, "roster.html", status_code=exc.http_status, people=_roster(db), roles=list(Role),
                      form={}, error=exc.message, tab="roster")
    db.commit()
    return _see_other("/roster")
