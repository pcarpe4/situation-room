"""Rules every event on an assignment shares, checked once per kind of event."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from situation_room import domain
from situation_room.domain import DomainError
from situation_room.models import Assignment, AuditLog, FiredrillState, Update, UpdateRequest

from .test_domain import assignment_of, make


def post_update(db, clock, actor, a, token):
    return domain.post_update(db, clock, actor, a.public_id, status="on_track", text="Two left.", form_token=token)


def set_wait(db, clock, actor, a, token):
    return domain.set_wait(db, clock, actor, a.public_id, until=clock.today() + timedelta(days=5),
                           reason="Vendor patch", form_token=token)


EVENTS = {"update": (post_update, "update.post"), "wait": (set_wait, "wait.set")}
kinds = pytest.mark.parametrize("kind", EVENTS)


@kinds
def test_a_repeated_send_returns_the_first(kind, db, clock, people):
    record, _ = EVENTS[kind]
    a = assignment_of(db, make(db, clock, people), people.ann)
    one = record(db, clock, people.ann, a, "same")
    db.commit()
    two = record(db, clock, people.ann, a, "same")
    db.commit()
    assert one.id == two.id
    assert db.scalar(select(func.count()).select_from(Update)) == 1


@kinds
def test_a_token_from_another_assignment_is_refused(kind, db, clock, people):
    record, _ = EVENTS[kind]
    f = make(db, clock, people, owners=[people.ann, people.bob])
    record(db, clock, people.lead, assignment_of(db, f, people.ann), "same")
    db.commit()
    with pytest.raises(DomainError) as err:
        record(db, clock, people.lead, assignment_of(db, f, people.bob), "same")
    assert err.value.code == "FORM_TOKEN_REUSED"


@kinds
def test_it_answers_every_open_ask_and_no_cancelled_one(kind, db, clock, people):
    record, _ = EVENTS[kind]
    a = assignment_of(db, make(db, clock, people), people.ann)
    first = domain.request_update(db, clock, people.lead, a.public_id)
    second = domain.request_update(db, clock, people.triage, a.public_id)
    cancelled = domain.request_update(db, clock, people.lead, a.public_id)
    domain.cancel_ask(db, clock, people.lead, cancelled.id)
    db.commit()
    event = record(db, clock, people.ann, a, "t")
    db.commit()
    for ask in (first, second, cancelled):
        db.refresh(ask)
    assert first.answered_by_update_id == event.id and second.answered_by_update_id == event.id
    assert cancelled.answered_by_update_id is None
    assert db.scalar(select(func.count()).select_from(UpdateRequest).where(UpdateRequest.is_open)) == 0


@kinds
def test_a_lead_posting_for_the_owner_is_marked_posted_for(kind, db, clock, people):
    record, _ = EVENTS[kind]
    a = assignment_of(db, make(db, clock, people), people.ann)
    event = record(db, clock, people.lead, a, "t")
    assert event.author_id == people.lead.id and event.on_behalf_of_id == people.ann.id
    own = record(db, clock, people.ann, a, "t2")
    assert own.on_behalf_of_id is None


@kinds
def test_a_manager_cannot_act_on_someone_elses_assignment(kind, db, clock, people):
    record, _ = EVENTS[kind]
    a = assignment_of(db, make(db, clock, people), people.ann)
    with pytest.raises(DomainError) as err:
        record(db, clock, people.bob, a, "t")
    assert err.value.http_status == 403


@kinds
def test_an_ended_assignment_is_refused_and_names_the_current_owners(kind, db, clock, people):
    record, _ = EVENTS[kind]
    f = make(db, clock, people, owners=[people.ann, people.bob])
    a = assignment_of(db, f, people.ann)
    a.ended_at = clock.now()
    db.commit()
    with pytest.raises(DomainError) as err:
        record(db, clock, people.ann, a, "t")
    assert err.value.code == "ASSIGNMENT_ENDED"
    assert "Bob Manager" in err.value.message


@kinds
def test_a_closed_firedrill_is_refused(kind, db, clock, people):
    record, _ = EVENTS[kind]
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    f.state = FiredrillState.CLOSED
    db.commit()
    with pytest.raises(DomainError) as err:
        record(db, clock, people.ann, a, "t")
    assert err.value.code == "FIREDRILL_CLOSED"


@kinds
def test_it_writes_one_audit_row(kind, db, clock, people):
    record, action = EVENTS[kind]
    a = assignment_of(db, make(db, clock, people), people.ann)
    record(db, clock, people.ann, a, "t")
    db.commit()
    rows = db.scalars(select(AuditLog).where(AuditLog.action == action)).all()
    assert [r.object_id for r in rows] == [str(a.public_id)]


def test_asking_is_refused_on_closed_firedrills_and_ended_assignments(db, clock, people):
    f = make(db, clock, people, owners=[people.ann, people.bob])
    a = assignment_of(db, f, people.ann)
    a.ended_at = clock.now()
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.request_update(db, clock, people.lead, a.public_id)
    assert err.value.code == "ASSIGNMENT_ENDED"
    db.rollback()
    f.state = FiredrillState.CLOSED
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.request_update(db, clock, people.lead, assignment_of(db, f, people.bob).public_id)
    assert err.value.code == "FIREDRILL_CLOSED"


def test_open_ask_and_active_assignment_mean_the_same_in_python_and_sql(db, clock, people):
    f = make(db, clock, people, owners=[people.ann, people.bob])
    a, b = assignment_of(db, f, people.ann), assignment_of(db, f, people.bob)
    ask = domain.request_update(db, clock, people.lead, a.public_id)
    b.ended_at = clock.now()
    db.commit()
    assert ask.is_open and a.is_active and not b.is_active
    assert db.scalars(select(Assignment.id).where(Assignment.is_active)).all() == [a.id]
    domain.cancel_ask(db, clock, people.lead, ask.id)
    db.commit()
    assert not ask.is_open
    assert db.scalar(select(func.count()).select_from(UpdateRequest).where(UpdateRequest.is_open)) == 0
