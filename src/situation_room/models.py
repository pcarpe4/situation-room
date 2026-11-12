"""Tables. Pages and jobs read these; only `situation_room.domain` writes assignment state.

Timestamps have no database default on purpose: they come from the app's clock.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    and_,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    MetaData,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Role(enum.StrEnum):
    LEAD = "lead"
    MANAGER = "manager"
    TRIAGE = "triage"


class Status(enum.StrEnum):
    NEW = "new"
    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    DONE = "done"


class FiredrillState(enum.StrEnum):
    OPEN = "open"
    CLOSED = "closed"


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(
        cls, name=name, native_enum=False, create_constraint=True, length=20,
        values_callable=lambda members: [m.value for m in members],
    )


class Person(Base):
    __tablename__ = "person"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320), unique=True)
    login_id: Mapped[str] = mapped_column(String(320), unique=True)
    role: Mapped[Role] = mapped_column(_enum(Role, "role"))
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        CheckConstraint("login_id = lower(login_id)", name="login_id_lowercase"),
    )


class Firedrill(Base):
    __tablename__ = "firedrill"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    short_description: Mapped[str] = mapped_column(String(500), default="")
    state: Mapped[FiredrillState] = mapped_column(_enum(FiredrillState, "firedrill_state"), default=FiredrillState.OPEN)
    due_date: Mapped[date | None] = mapped_column(Date)
    requested_by: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(200))
    ticket_refs: Mapped[str] = mapped_column(String(500), default="")
    auto_ask: Mapped[bool] = mapped_column(default=True)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("person.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    assignments: Mapped[list[Assignment]] = relationship(back_populates="firedrill", order_by="Assignment.id")


class Assignment(Base):
    __tablename__ = "assignment"

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, default=uuid.uuid4)
    firedrill_id: Mapped[int] = mapped_column(ForeignKey("firedrill.id"))
    owner_id: Mapped[int] = mapped_column(ForeignKey("person.id"))
    status: Mapped[Status] = mapped_column(_enum(Status, "status"), default=Status.NEW)
    waiting_until: Mapped[date | None] = mapped_column(Date)
    waiting_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    firedrill: Mapped[Firedrill] = relationship(back_populates="assignments")
    owner: Mapped[Person] = relationship()

    @hybrid_property
    def is_active(self) -> bool:
        """Not ended by a removal or reassignment. Done assignments are still active."""
        return self.ended_at is None

    @is_active.inplace.expression
    @classmethod
    def _is_active_sql(cls):
        return cls.ended_at.is_(None)

    __table_args__ = (
        Index(
            "uq_assignment_one_unended_per_owner", "firedrill_id", "owner_id",
            unique=True, postgresql_where=text("ended_at IS NULL"),
        ),
        Index("ix_assignment_owner_unended", "owner_id", postgresql_where=text("ended_at IS NULL")),
    )


class Update(Base):
    __tablename__ = "update"

    # The id is a database sequence and breaks ties for "newest update".
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignment.id"))
    author_id: Mapped[int] = mapped_column(ForeignKey("person.id"))
    on_behalf_of_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    status: Mapped[Status | None] = mapped_column(_enum(Status, "update_status"))
    text: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(20), default="form")
    form_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    author: Mapped[Person] = relationship(foreign_keys=[author_id])

    __table_args__ = (Index("ix_update_assignment_newest", "assignment_id", "id"),)


class UpdateRequest(Base):
    """An ask. In M1 it is a prefilled email the asker sends from their own mail program."""

    __tablename__ = "update_request"

    id: Mapped[int] = mapped_column(primary_key=True)
    assignment_id: Mapped[int] = mapped_column(ForeignKey("assignment.id"))
    asked_by_id: Mapped[int] = mapped_column(ForeignKey("person.id"))
    origin: Mapped[str] = mapped_column(String(10), default="manual")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    confirmed_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_reason: Mapped[str | None] = mapped_column(String(30))
    answered_by_update_id: Mapped[int | None] = mapped_column(ForeignKey("update.id"))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)

    @hybrid_property
    def is_open(self) -> bool:
        """Neither answered by an update nor cancelled. The partial index below uses the same rule."""
        return self.answered_by_update_id is None and self.cancelled_at is None

    @is_open.inplace.expression
    @classmethod
    def _is_open_sql(cls):
        return and_(cls.answered_by_update_id.is_(None), cls.cancelled_at.is_(None))

    __table_args__ = (
        Index(
            "ix_update_request_open", "assignment_id",
            postgresql_where=text("answered_by_update_id IS NULL AND cancelled_at IS NULL"),
        ),
    )


class AuditLog(Base):
    """Who changed what. Holds ids and field names, never text taken from email."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("person.id"))
    action: Mapped[str] = mapped_column(String(50))
    object_type: Mapped[str] = mapped_column(String(30))
    object_id: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UsageEvent(Base):
    """One row per person per day per kind. Feeds the "opens the board" figure."""

    __tablename__ = "usage_event"

    person_id: Mapped[int] = mapped_column(ForeignKey("person.id"), primary_key=True)
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), primary_key=True)
