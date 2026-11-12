import threading
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from situation_room import board, domain
from situation_room.domain import DomainError
from situation_room.models import Assignment, AuditLog, Status, Update, UpdateRequest


def make(db, clock, people, owners=None, **kw):
    fields = dict(title="Rotate passwords", short_description="Rotate the flagged service accounts.",
                  due_date=None, requested_by=None, source="Security", ticket_text="see SCTASK0012345 and INC0099887")
    fields.update(kw)
    f = domain.create_firedrill(db, clock, people.lead, owner_ids=[p.id for p in (owners or [people.ann])], **fields)
    db.commit()
    return f


def assignment_of(db, f, person):
    return db.scalar(select(Assignment).where(Assignment.firedrill_id == f.id, Assignment.owner_id == person.id))


def test_create_needs_an_owner_and_a_title(db, clock, people):
    with pytest.raises(DomainError) as err:
        domain.create_firedrill(db, clock, people.lead, title="X", short_description="Y", due_date=None,
                                requested_by=None, source=None, ticket_text="", owner_ids=[])
    assert err.value.code == "NO_OWNER"
    with pytest.raises(DomainError) as err:
        domain.create_firedrill(db, clock, people.lead, title="  ", short_description="Y", due_date=None,
                                requested_by=None, source=None, ticket_text="", owner_ids=[people.ann.id])
    assert err.value.code == "FIELD_REQUIRED"


def test_managers_cannot_create_firedrills(db, clock, people):
    with pytest.raises(DomainError) as err:
        domain.create_firedrill(db, clock, people.ann, title="X", short_description="Y", due_date=None,
                                requested_by=None, source=None, ticket_text="", owner_ids=[people.ann.id])
    assert err.value.http_status == 403


def test_triage_cannot_be_an_owner(db, clock, people):
    with pytest.raises(DomainError) as err:
        make(db, clock, people, owners=[people.triage])
    assert err.value.code == "OWNER_INVALID"


def test_ticket_refs_use_the_longest_prefix(db, clock, people):
    f = make(db, clock, people)
    assert f.ticket_refs == "SCTASK0012345 INC0099887"


def test_an_update_sets_the_status_and_done_clears_a_wait(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    domain.set_wait(db, clock, people.ann, a.public_id, until=clock.today() + timedelta(days=3), reason="Vendor")
    domain.post_update(db, clock, people.ann, a.public_id, status="at_risk", text="Two left.", form_token="t1")
    db.commit()
    db.refresh(a)
    assert a.status == Status.AT_RISK and a.waiting_until is not None
    domain.post_update(db, clock, people.ann, a.public_id, status="done", text="All done.", form_token="t2")
    db.commit()
    db.refresh(a)
    assert a.status == Status.DONE and a.waiting_until is None and a.waiting_reason is None


def test_two_simultaneous_sends_with_one_token_make_one_update(sm, db, clock, people):
    f = make(db, clock, people)
    public_id = assignment_of(db, f, people.ann).public_id
    ann_id = people.ann.id
    barrier = threading.Barrier(2)
    results, errors = [], []

    def send():
        with sm() as s:
            ann = s.get(type(people.ann), ann_id)
            barrier.wait()
            try:
                results.append(domain.post_update(s, clock, ann, public_id, status="on_track", text="x", form_token="race").id)
                s.commit()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(results)) == 1
    assert db.scalar(select(func.count()).select_from(Update)) == 1








def test_a_status_is_required(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    for bad in ("", "new", "maybe"):
        with pytest.raises(DomainError) as err:
            domain.post_update(db, clock, people.ann, a.public_id, status=bad, text="", form_token="")
        assert err.value.code == "STATUS_REQUIRED"
        db.rollback()


def test_a_wait_must_be_future_and_explained(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    with pytest.raises(DomainError):
        domain.set_wait(db, clock, people.ann, a.public_id, until=clock.today(), reason="vendor")
    db.rollback()
    with pytest.raises(DomainError):
        domain.set_wait(db, clock, people.ann, a.public_id, until=clock.today() + timedelta(days=3), reason=" ")
    db.rollback()
    domain.set_wait(db, clock, people.ann, a.public_id, until=clock.today() + timedelta(days=3), reason="Vendor patch")
    db.commit()
    db.refresh(a)
    assert a.waiting_reason == "Vendor patch"


def test_done_assignments_cannot_be_asked(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    domain.post_update(db, clock, people.ann, a.public_id, status="done", text="", form_token="t")
    db.commit()
    with pytest.raises(DomainError) as err:
        domain.request_update(db, clock, people.lead, a.public_id)
    assert err.value.code == "ASSIGNMENT_DONE"


def test_managers_cannot_ask(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    with pytest.raises(DomainError) as err:
        domain.request_update(db, clock, people.bob, a.public_id)
    assert err.value.http_status == 403


def test_deactivation_is_refused_while_someone_owns_active_work(db, clock, people):
    make(db, clock, people)
    with pytest.raises(DomainError) as err:
        domain.deactivate_person(db, clock, people.triage, people.ann.id)
    assert err.value.code == "PERSON_HAS_ACTIVE_WORK"
    db.rollback()
    domain.deactivate_person(db, clock, people.triage, people.bob.id)
    db.commit()
    assert not people.bob.active


def test_the_last_triage_person_cannot_be_deactivated(db, clock, people):
    with pytest.raises(DomainError) as err:
        domain.deactivate_person(db, clock, people.triage, people.triage.id)
    assert err.value.code == "LAST_TRIAGE"


def test_roster_addresses_are_unique_regardless_of_case(db, clock, people):
    with pytest.raises(DomainError) as err:
        domain.add_person(db, clock, people.triage, display_name="Ann Again", email="ANN.MANAGER@example.com",
                          login_id="someone.else", role="manager")
    assert err.value.code == "EMAIL_TAKEN"


def test_first_triage_same_mailbox_corrects_the_login(db, clock, people):
    p = domain.ensure_first_triage(db, clock, login_id="TARA2", email="tara.triage@example.com", display_name="Tara")
    assert p.id == people.triage.id
    assert p.login_id == "tara2"
    other = domain.ensure_first_triage(db, clock, login_id="new.person", email="new.person@example.com", display_name="New")
    assert other.id != people.triage.id


def test_the_audit_log_holds_ids_not_text(db, clock, people):
    f = make(db, clock, people)
    a = assignment_of(db, f, people.ann)
    domain.post_update(db, clock, people.ann, a.public_id, status="on_track", text="secret-ish words", form_token="t")
    db.commit()
    for entry in db.scalars(select(AuditLog)):
        assert "secret-ish" not in str(entry.detail)
        assert "Rotate passwords" not in str(entry.detail)


def test_board_marks_stale_waiting_and_sorts_stale_first(db, clock, people):
    stale = make(db, clock, people, title="Old one", owners=[people.ann])
    fresh_blocked = make(db, clock, people, title="Blocked one", owners=[people.bob])
    clock.advance(days=7)  # Wednesday to the next Wednesday: five working days.
    b = assignment_of(db, fresh_blocked, people.bob)
    domain.post_update(db, clock, people.bob, b.public_id, status="blocked", text="", form_token="b")
    db.commit()

    view = board.build(db, clock, 3)
    assert [r.title for r in view.rows] == ["Old one", "Blocked one"]
    assert view.rows[0].lines[0].stale and view.rows[0].lines[0].stale_days == 5
    assert view.counts["stale"] == 1

    a = assignment_of(db, stale, people.ann)
    domain.set_wait(db, clock, people.ann, a.public_id, until=clock.today() + timedelta(days=2), reason="Vendor")
    db.commit()
    view = board.build(db, clock, 3)
    assert view.counts["stale"] == 0 and view.counts["waiting"] == 1


def test_rollup_ignores_done_unless_all_done():
    assert board.rollup([Status.DONE, Status.ON_TRACK, Status.NEW]) == Status.NEW
    assert board.rollup([Status.ON_TRACK, Status.BLOCKED]) == Status.BLOCKED
    assert board.rollup([Status.DONE, Status.DONE]) == Status.DONE


def test_board_uses_a_fixed_number_of_queries(engine, sm, clock, people):
    from sqlalchemy import event

    def count_queries():
        n = 0

        def bump(*_):
            nonlocal n
            n += 1
        event.listen(engine, "before_cursor_execute", bump)
        with sm() as s:
            board.build(s, clock, 3)
        event.remove(engine, "before_cursor_execute", bump)
        return n

    with sm() as s:
        lead = s.get(type(people.lead), people.lead.id)
        owners = [people.ann.id, people.bob.id]
        for i in range(3):
            domain.create_firedrill(s, clock, lead, title=f"F{i}", short_description="d", due_date=None,
                                    requested_by=None, source=None, ticket_text="", owner_ids=owners)
        s.commit()
    small = count_queries()
    with sm() as s:
        lead = s.get(type(people.lead), people.lead.id)
        for i in range(27):
            f = domain.create_firedrill(s, clock, lead, title=f"G{i}", short_description="d", due_date=None,
                                        requested_by=None, source=None, ticket_text="", owner_ids=owners)
            a = s.scalar(select(Assignment).where(Assignment.firedrill_id == f.id))
            domain.post_update(s, clock, lead, a.public_id, status="on_track", text="", form_token=uuid.uuid4().hex)
            domain.request_update(s, clock, lead, a.public_id)
        s.commit()
    assert count_queries() == small
