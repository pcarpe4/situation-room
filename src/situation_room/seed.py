"""Sample data for local runs: a board with every state worth looking at.

Everyone here is fictional and every address is at example.com.
Built through the domain module, with the clock set back for each step.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import domain
from .clock import Clock, FixedClock
from .dates import subtract_working_days, week_end
from .models import Firedrill, Person, Role, Status

PEOPLE = (
    ("Sam Rivera", "sam.rivera@example.com", "sam.rivera", Role.TRIAGE),
    ("Robin Ortiz", "robin.ortiz@example.com", "robin.ortiz", Role.LEAD),
    ("Alex Kim", "alex.kim@example.com", "alex.kim", Role.MANAGER),
    ("Jordan Lee", "jordan.lee@example.com", "jordan.lee", Role.MANAGER),
    ("Priya Shah", "priya.shah@example.com", "priya.shah", Role.MANAGER),
    ("Morgan Diaz", "morgan.diaz@example.com", "morgan.diaz", Role.MANAGER),
)


def seed(s: Session, clock: Clock) -> bool:
    """Load the sample board. Returns False when firedrills already exist."""
    if s.scalar(select(func.count()).select_from(Firedrill)):
        return False

    tz: ZoneInfo = clock.tz
    today = clock.today()

    def at(day: date, hour: int = 10) -> FixedClock:
        return FixedClock(tz, datetime.combine(day, time(hour), tz))

    def wd(n: int) -> date:
        return subtract_working_days(today, n)

    def token() -> str:
        return uuid.uuid4().hex

    people: dict[str, Person] = {}
    for name, email, login, role in PEOPLE:
        existing = s.scalar(select(Person).where(Person.email == email))
        people[name.split()[0]] = existing or domain.add_person(
            s, at(wd(20)), None, display_name=name, email=email, login_id=login, role=role)
    lead, alex, jordan, priya, morgan = (people[k] for k in ("Robin", "Alex", "Jordan", "Priya", "Morgan"))

    def lines(firedrill: Firedrill) -> dict[int, uuid.UUID]:
        s.refresh(firedrill)
        return {a.owner_id: a.public_id for a in firedrill.assignments}

    tests = domain.create_firedrill(
        s, at(wd(8)), lead, title="Functional tests for every application",
        short_description="Every application team adds functional tests to its pipeline before the quarter closes.",
        due_date=today + timedelta(days=20), requested_by="Platform engineering", source="Platform engineering",
        ticket_text="RITM0045121", owner_ids=[alex.id, jordan.id, priya.id, morgan.id],
    )
    a = lines(tests)
    domain.post_update(s, at(wd(3)), morgan, a[morgan.id], status=Status.DONE,
                       text="All four of our apps run their tests on every merge.", form_token=token())
    domain.post_update(s, at(wd(2)), jordan, a[jordan.id], status=Status.BLOCKED,
                       text="Waiting on test accounts from the identity team. Request is in.", form_token=token())
    domain.post_update(s, at(wd(1), 15), alex, a[alex.id], status=Status.ON_TRACK,
                       text="Three of five apps have tests in the pipeline. The last two land next week.", form_token=token())

    rotation = domain.create_firedrill(
        s, at(wd(10)), lead, title="Rotate service account passwords",
        short_description="Rotate every service account password flagged in the audit and confirm nothing breaks.",
        due_date=today - timedelta(days=2), requested_by="Security office", source="Security office",
        ticket_text="CHG0034567", owner_ids=[jordan.id],
    )
    r = lines(rotation)
    domain.post_update(s, at(wd(6)), jordan, r[jordan.id], status=Status.AT_RISK,
                       text="Two accounts still have hard-coded passwords in batch jobs.", form_token=token())
    ask = domain.request_update(s, at(wd(1), 9), lead, r[jordan.id])
    domain.confirm_ask_sent(s, at(wd(1), 9), lead, ask.id)

    patch = domain.create_firedrill(
        s, at(wd(4)), lead, title="INC0012345 follow-up: vendor patch",
        short_description="Apply the vendor's fix for last week's outage and close the incident.",
        due_date=today + timedelta(days=12), requested_by="Service desk", source="Service desk",
        ticket_text="INC0012345", owner_ids=[priya.id],
    )
    p = lines(patch)
    domain.post_update(s, at(wd(3)), priya, p[priya.id], status=Status.ON_TRACK,
                       text="Change request approved.", form_token=token())
    domain.set_wait(s, at(wd(1)), priya, p[priya.id], until=today + timedelta(days=5),
                    reason="The vendor ships the patch next week.", form_token=token())

    domain.create_firedrill(
        s, at(today, 9), lead, title="Onboard tests to the new test platform",
        short_description="Move existing test suites onto the shared test platform.",
        due_date=max(today, week_end(today) - timedelta(days=2)), requested_by="Quality team",
        source="Quality team", ticket_text="", owner_ids=[alex.id],
    )
    return True
