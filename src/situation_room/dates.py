"""The one place that does day arithmetic: stale, waiting, overdue and due this week.

Days are calendar dates in the app's time zone. Working days are Monday to
Friday, minus any listed non-working dates.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, datetime, timedelta, tzinfo

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


@dataclass(frozen=True)
class StaleRules:
    """What the stale rule needs besides the assignment: the app's time zone, the
    number of working days an owner gets (SR_STALE_AFTER) and any non-working dates."""

    tz: tzinfo
    stale_after: int
    non_working: frozenset[date] = NO_DATES


@dataclass(frozen=True)
class Staleness:
    waiting: bool
    stale: bool
    stale_days: int  # working days since `clock_start`; 0 unless stale
    clock_start: date


def staleness(
    *, created_at: datetime, newest_update_at: datetime | None, waiting_until: date | None,
    done: bool, today: date, rules: StaleRules,
) -> Staleness:
    """Whether an assignment is waiting or stale today. The one place that decides it.

    - Timestamps become calendar dates in the app's time zone.
    - The clock counts from the newest update, or from when it was assigned. Once a
      wait is over, it counts from the wait's last day if that is later.
    - A wait runs through its date. A waiting or done assignment is never stale.
    - Stale from working day N+1 after the clock start: with N=3, an update on
      Monday makes the assignment stale on Friday morning.
    """
    start = (newest_update_at or created_at).astimezone(rules.tz).date()
    if waiting_until is not None and waiting_until < today:
        start = max(start, waiting_until)
    waiting = not done and waiting_until is not None and today <= waiting_until
    days = working_days_after(start, today, rules.non_working)
    stale = not done and not waiting and days >= rules.stale_after + 1
    return Staleness(waiting=waiting, stale=stale, stale_days=days if stale else 0, clock_start=start)


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
