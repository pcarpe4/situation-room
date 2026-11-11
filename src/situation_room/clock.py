"""The app's clock. Every business timestamp comes from here, never from the database.

Development can shift it with SR_CLOCK_OVERRIDE (refused outside dev sign-in);
tests pin it with FixedClock.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


class Clock:
    def __init__(self, tz: ZoneInfo, override: datetime | None = None):
        self.tz = tz
        self._offset = (override - datetime.now(timezone.utc)) if override else timedelta(0)

    def now(self) -> datetime:
        return datetime.now(timezone.utc) + self._offset

    def today(self) -> date:
        return self.now().astimezone(self.tz).date()

    def local(self, moment: datetime) -> datetime:
        return moment.astimezone(self.tz)


class FixedClock(Clock):
    """A clock that only moves when told to."""

    def __init__(self, tz: ZoneInfo, at: datetime):
        super().__init__(tz)
        self._at = at if at.tzinfo else at.replace(tzinfo=tz)

    def now(self) -> datetime:
        return self._at.astimezone(timezone.utc)

    def set(self, at: datetime) -> None:
        self._at = at if at.tzinfo else at.replace(tzinfo=self.tz)

    def advance(self, **kwargs: float) -> None:
        self._at = self._at + timedelta(**kwargs)
