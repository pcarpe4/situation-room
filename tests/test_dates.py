from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from situation_room import dates

MON = date(2026, 11, 9)
TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(10, 16))
NEXT_MON = date(2026, 11, 16)


CHICAGO = ZoneInfo("America/Chicago")
N3 = dates.StaleRules(tz=CHICAGO, stale_after=3)


def at(day: date, hour: int = 10, minute: int = 0) -> datetime:
    """A moment on `day` in Chicago, as the database would hand it back (UTC)."""
    return datetime.combine(day, time(hour, minute), CHICAGO).astimezone(timezone.utc)


def check(today, *, created=MON, updated=None, waiting_until=None, done=False, rules=N3):
    return dates.staleness(created_at=at(created), newest_update_at=updated, waiting_until=waiting_until,
                           done=done, today=today, rules=rules)


@pytest.mark.parametrize(("today", "stale", "days"), [
    (TUE, False, 0), (WED, False, 0), (THU, False, 0), (FRI, True, 4), (SAT, True, 4), (NEXT_MON, True, 5),
])
def test_monday_update_turns_stale_on_friday_with_n3(today, stale, days):
    result = check(today, updated=at(MON))
    assert (result.stale, result.stale_days) == (stale, days)
    assert result.clock_start == MON


def test_with_n1_a_monday_update_is_stale_on_wednesday():
    rules = dates.StaleRules(tz=CHICAGO, stale_after=1)
    assert not check(TUE, updated=at(MON), rules=rules).stale
    assert check(WED, updated=at(MON), rules=rules).stale


def test_no_update_counts_from_when_it_was_assigned():
    assert check(FRI, created=MON).stale
    assert not check(FRI, created=TUE).stale


def test_friday_update_skips_the_weekend():
    # Fri update: Mon(1) Tue(2) Wed(3) not stale, Thu(4) stale.
    assert not check(date(2026, 11, 18), created=FRI, updated=at(FRI)).stale
    assert check(date(2026, 11, 19), created=FRI, updated=at(FRI)).stale


def test_the_day_of_an_update_is_its_local_date_not_its_utc_date():
    # 23:30 Tuesday in Chicago is already Wednesday in UTC. It counts as Tuesday.
    late_tuesday = at(TUE, 23, 30)
    assert late_tuesday.date() == WED
    n1 = dates.StaleRules(tz=CHICAGO, stale_after=1)
    assert check(THU, updated=late_tuesday, rules=n1).stale
    # 00:30 Wednesday counts as Wednesday.
    assert not check(THU, updated=at(WED, 0, 30), rules=n1).stale


def test_a_holiday_inside_the_window_pushes_stale_back_one_day():
    holiday = dates.StaleRules(tz=CHICAGO, stale_after=3, non_working=frozenset({WED}))
    assert check(FRI, updated=at(MON)).stale
    result = check(FRI, updated=at(MON), rules=holiday)
    assert not result.stale and result.stale_days == 0
    assert check(NEXT_MON, updated=at(MON), rules=holiday).stale


def test_a_wait_runs_through_its_date_and_is_never_stale():
    result = check(NEXT_MON, updated=at(MON), waiting_until=NEXT_MON)
    assert result.waiting and not result.stale and result.stale_days == 0


def test_after_a_wait_ends_the_clock_counts_from_its_last_day():
    # Waited through Thursday; the older Monday update no longer counts.
    result = check(NEXT_MON, updated=at(MON), waiting_until=THU)
    assert not result.waiting and result.clock_start == THU and not result.stale
    assert check(date(2026, 11, 20), updated=at(MON), waiting_until=THU).stale
    # An update newer than the end of the wait wins.
    assert check(SAT, updated=at(FRI), waiting_until=THU).clock_start == FRI


def test_a_done_assignment_is_neither_stale_nor_waiting():
    result = check(date(2026, 12, 1), updated=at(MON), waiting_until=date(2026, 12, 5), done=True)
    assert not result.stale and not result.waiting and result.stale_days == 0
    assert not check(date(2026, 12, 1), updated=at(MON), done=True).stale


def test_overdue_starts_the_day_after_the_due_date():
    assert dates.overdue_days(WED, WED) == 0
    assert dates.overdue_days(WED, THU) == 1
    assert dates.overdue_days(WED, NEXT_MON) == 5
    assert dates.overdue_days(None, NEXT_MON) == 0


def test_due_this_week_runs_today_through_sunday():
    assert dates.is_due_this_week(WED, WED)
    assert dates.is_due_this_week(SUN, WED)
    assert not dates.is_due_this_week(NEXT_MON, WED)
    assert not dates.is_due_this_week(TUE, WED)


def test_subtract_working_days_skips_weekends():
    assert dates.subtract_working_days(NEXT_MON, 1) == FRI
    assert dates.subtract_working_days(WED, 3) == date(2026, 11, 6)
