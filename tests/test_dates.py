from datetime import date

import pytest

from situation_room import dates

MON = date(2026, 11, 9)
TUE, WED, THU, FRI, SAT, SUN = (date(2026, 11, d) for d in range(10, 16))
NEXT_MON = date(2026, 11, 16)


@pytest.mark.parametrize(("today", "stale"), [
    (TUE, False), (WED, False), (THU, False), (FRI, True), (SAT, True), (NEXT_MON, True),
])
def test_monday_update_turns_stale_on_friday_with_n3(today, stale):
    assert dates.is_stale(MON, today, 3) is stale


def test_friday_update_skips_the_weekend():
    # Fri update: Mon(1) Tue(2) Wed(3) not stale, Thu(4) stale.
    assert not dates.is_stale(FRI, date(2026, 11, 18), 3)
    assert dates.is_stale(FRI, date(2026, 11, 19), 3)


def test_non_working_dates_push_stale_back_one_day():
    assert dates.is_stale(MON, FRI, 3)
    assert not dates.is_stale(MON, FRI, 3, non_working={WED})


def test_waiting_runs_through_its_date_then_counts_from_it():
    assert dates.is_waiting(THU, THU)
    assert not dates.is_waiting(THU, FRI)
    # A wait until Thursday ends; the clock starts from Thursday, not the older update.
    assert dates.clock_start(created=MON, newest_update=MON, waiting_until=THU, today=FRI) == THU
    # While the wait runs, the clock still points at the newest update.
    assert dates.clock_start(created=MON, newest_update=TUE, waiting_until=THU, today=WED) == TUE
    # An update newer than the end of the wait wins.
    assert dates.clock_start(created=MON, newest_update=FRI, waiting_until=THU, today=SAT) == FRI


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
