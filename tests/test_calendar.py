from datetime import date

from situation_room import board, domain
from situation_room.models import Firedrill, FiredrillState

from .conftest import sign_in


def make(db, clock, people, title, due, owners=None):
    f = domain.create_firedrill(db, clock, people.lead, title=title, short_description="x", due_date=due,
                                requested_by=None, source=None, ticket_text="",
                                owner_ids=[p.id for p in (owners or [people.ann])])
    db.commit()
    return f


def test_parse_month_falls_back_to_this_month():
    today = date(2026, 11, 11)
    assert board.parse_month("2027-02", today) == date(2027, 2, 1)
    for bad in (None, "", "2026-13", "nope", "2026-11-05"):
        assert board.parse_month(bad, today) == date(2026, 11, 1)


def test_month_grid_starts_monday_and_places_due_dates_and_waits(db, clock, people):
    make(db, clock, people, "Rotate keys", date(2026, 11, 20))
    make(db, clock, people, "Overdue thing", date(2026, 11, 2))
    make(db, clock, people, "Next month", date(2026, 12, 3))
    make(db, clock, people, "Someday", None)
    waiting = make(db, clock, people, "Vendor fix", date(2026, 11, 30), owners=[people.bob])
    domain.set_wait(db, clock, people.bob, waiting.assignments[0].public_id, until=date(2026, 11, 18), reason="Vendor")
    db.commit()

    cal = board.calendar(db, clock, 3, people.lead, date(2026, 11, 1))
    days = {c.day: c for week in cal.weeks for c in week}
    assert all(week[0].day.weekday() == 0 for week in cal.weeks)
    assert cal.weeks[0][0].day == date(2026, 10, 26) and not cal.weeks[0][0].in_month
    assert [r.title for r in days[date(2026, 11, 20)].due] == ["Rotate keys"]
    assert days[date(2026, 11, 2)].due[0].overdue_days == 9
    assert days[date(2026, 11, 11)].is_today
    assert [w.owner_name for w in days[date(2026, 11, 18)].waits] == ["Bob Manager"]
    assert [r.title for r in days[date(2026, 12, 3)].due] == ["Next month"]  # shown in the trailing week
    assert cal.due_count == 3
    assert [r.title for r in cal.undated] == ["Someday"]
    assert (cal.prev_month, cal.next_month) == (date(2026, 10, 1), date(2026, 12, 1))


def test_closed_firedrills_stay_off_the_calendar(db, clock, people):
    make(db, clock, people, "Gone", date(2026, 11, 20))
    db.execute(Firedrill.__table__.update().values(state=FiredrillState.CLOSED))
    db.commit()
    cal = board.calendar(db, clock, 3, people.lead, date(2026, 11, 1))
    assert cal.due_count == 0 and not any(c.due for week in cal.weeks for c in week)


def test_calendar_page_renders_for_a_manager(db, clock, client, people):
    make(db, clock, people, "<b>Rotate</b> keys", date(2026, 11, 20))
    sign_in(client, people.ann)
    html = client.get("/calendar").text
    assert "November 2026" in html and "&lt;b&gt;Rotate&lt;/b&gt; keys" in html
    assert 'href="/calendar?month=2026-12"' in html and "This month" not in html
    html = client.get("/calendar?month=2027-01").text
    assert "January 2027" in html and 'href="/calendar"' in html and "0 open firedrills due in January" in html
