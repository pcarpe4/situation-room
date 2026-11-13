"""The board's "Needs you" queue: which owner lines need the viewer now, and the done-today count."""

from datetime import date, datetime

from situation_room import board, domain
from situation_room.board import NeedKind as N

from .conftest import sign_in
from .test_domain import assignment_of, make


def queue(db, clock, viewer):
    view = board.build(db, clock, 3, viewer)
    return [(n.kind, n.line.owner_name) for n in view.needs], view.done_today


def quiet_firedrill(db, clock, people, **kw):
    """Assigned Monday Nov 2; by Wednesday Nov 11 the owner is seven working days quiet."""
    clock.set(datetime(2026, 11, 2, 10))
    f = make(db, clock, people, **kw)
    clock.set(datetime(2026, 11, 11, 10))
    return f


def test_a_quiet_owner_moves_through_request_confirm_and_ask_again(db, clock, people):
    f = quiet_firedrill(db, clock, people)
    a = assignment_of(db, f, people.ann)
    assert queue(db, clock, people.lead) == ([(N.REQUEST, "Ann Manager")], 0)

    ask = domain.request_update(db, clock, people.lead, a.public_id)
    db.commit()
    assert queue(db, clock, people.lead) == ([(N.CONFIRM, "Ann Manager")], 0)

    domain.confirm_ask_sent(db, clock, people.lead, ask.id)
    db.commit()
    assert queue(db, clock, people.lead) == ([], 1)

    clock.set(datetime(2026, 11, 12, 10))  # one working day: still the owner's turn
    assert queue(db, clock, people.lead) == ([], 0)
    clock.set(datetime(2026, 11, 13, 10))  # two working days with no reply
    assert queue(db, clock, people.lead) == ([(N.ASK_AGAIN, "Ann Manager")], 0)


def test_an_overdue_firedrill_comes_back_after_one_working_day_and_first(db, clock, people):
    later = quiet_firedrill(db, clock, people, title="Later", owners=[people.bob], due_date=date(2026, 12, 1))
    late = quiet_firedrill(db, clock, people, title="Late", due_date=date(2026, 11, 9))
    for f, person in ((late, people.ann), (later, people.bob)):
        ask = domain.request_update(db, clock, people.lead, assignment_of(db, f, person).public_id)
        domain.confirm_ask_sent(db, clock, people.lead, ask.id)
    db.commit()

    clock.set(datetime(2026, 11, 12, 10))
    assert queue(db, clock, people.lead)[0] == [(N.ASK_AGAIN, "Ann Manager")]
    clock.set(datetime(2026, 11, 13, 10))
    assert queue(db, clock, people.lead)[0] == [(N.ASK_AGAIN, "Ann Manager"), (N.ASK_AGAIN, "Bob Manager")]


def test_an_owner_is_asked_for_their_own_update_and_it_counts_once_posted(db, clock, people):
    f = quiet_firedrill(db, clock, people)
    a = assignment_of(db, f, people.ann)
    assert queue(db, clock, people.ann) == ([(N.UPDATE, "Ann Manager")], 0)
    domain.post_update(db, clock, people.ann, a.public_id, status="on_track", text="Half done.", form_token="t")
    db.commit()
    assert queue(db, clock, people.ann) == ([], 1)


def test_blocked_needs_a_nudge_and_waiting_does_not(db, clock, people):
    f = make(db, clock, people, owners=[people.ann, people.bob])
    domain.post_update(db, clock, people.ann, assignment_of(db, f, people.ann).public_id,
                       status="blocked", text="Waiting on accounts.", form_token="b")
    domain.set_wait(db, clock, people.bob, assignment_of(db, f, people.bob).public_id,
                    until=date(2026, 11, 20), reason="Vendor", form_token="w")
    db.commit()
    assert queue(db, clock, people.lead) == ([(N.BLOCKED, "Ann Manager")], 0)


def test_the_board_page_leads_with_the_queue(db, clock, client, people):
    f = quiet_firedrill(db, clock, people, title="Rotate passwords", due_date=date(2026, 11, 13))
    sign_in(client, people.lead)
    html = client.get("/board").text
    assert "1 thing needs you today" in html and "One owner has gone quiet." in html
    assert "<strong>Ann Manager</strong> has gone quiet." in html and "Start here" in html
    assert f'action="/a/{assignment_of(db, f, people.ann).public_id}/ask"' in html
    assert "0 of 1 done today" in html
    assert "Wed Nov 11, today" in html and "Fri Nov 13: Rotate passwords due" in html


def test_focus_mode_tucks_the_list_away_and_stays_on(db, clock, client, people):
    make(db, clock, people, title="Rotate passwords")
    token = sign_in(client, people.lead)
    assert 'class="fd-list"' in client.get("/board").text

    r = client.post("/focus", data={"csrf_token": token, "focus": "on", "back": "/board"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/board"
    html = client.get("/board").text
    assert 'class="fd-list"' not in html and "1 open firedrill is tucked away while you focus." in html
    assert 'aria-pressed="true"' in html

    client.post("/focus", data={"csrf_token": token, "focus": "off", "back": "/board"})
    assert 'class="fd-list"' in client.get("/board").text
