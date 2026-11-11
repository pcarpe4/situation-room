"""The one place that does day arithmetic: stale, waiting, overdue and due this week.

Days are calendar dates in the app's time zone. Working days are Monday to
Friday, minus any listed non-working dates.
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import date, timedelta

NO_DATES: frozenset[date] = frozenset()


def is_working_day(day: date, non_working: Collection[date] = NO_DATES) -> bool:
    return day.weekday() < 5 and day not in non_working


def working_days_after(start: date, end: date, non_working: Collection[date] = NO_DATES) -> int:
    """Count working days in (start, end]. Zero when end is not after start."""
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_working_day(day, non_working):
            count += 1
        day += timedelta(days=1)
    return count


def is_waiting(waiting_until: date | None, today: date) -> bool:
    """An assignment waits through the waiting_until date itself."""
    return waiting_until is not None and today <= waiting_until


def clock_start(created: date, newest_update: date | None, waiting_until: date | None, today: date) -> date:
    """The date the stale clock counts from.

    It is the newest update (or the assignment's creation), moved forward to
    the end of a wait once that wait is over.
    """
    start = newest_update or created
    if waiting_until is not None and waiting_until < today:
        start = max(start, waiting_until)
    return start


def stale_working_days(start: date, today: date, non_working: Collection[date] = NO_DATES) -> int:
    return working_days_after(start, today, non_working)


def is_stale(start: date, today: date, stale_after: int, non_working: Collection[date] = NO_DATES) -> bool:
    """Stale from the start of working day N+1 after the clock start.

    With N=3, an update on Monday makes the assignment stale on Friday morning.
    """
    return working_days_after(start, today, non_working) >= stale_after + 1


def overdue_days(due: date | None, today: date) -> int:
    """Calendar days past due, or 0. Overdue starts the day after the due date."""
    if due is None or today <= due:
        return 0
    return (today - due).days


def week_end(today: date) -> date:
    """The Sunday that ends today's Monday-to-Sunday week."""
    return today + timedelta(days=6 - today.weekday())


def is_due_this_week(due: date | None, today: date) -> bool:
    return due is not None and today <= due <= week_end(today)


def subtract_working_days(day: date, count: int, non_working: Collection[date] = NO_DATES) -> date:
    """The working day `count` working days before `day`."""
    while count > 0:
        day -= timedelta(days=1)
        if is_working_day(day, non_working):
            count -= 1
    return day
