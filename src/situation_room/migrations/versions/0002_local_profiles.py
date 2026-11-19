"""local profiles: a password per person, and the sample board removed

Adds `person.password_hash`. Also removes the sample data that earlier versions
loaded (`sr seed`): the four sample firedrills with everything on them, and the
six sample people. A sample person that real work still points at is kept, but
switched to inactive, so nothing a person entered is lost.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19 10:00:00
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = '0002'
down_revision: str | None = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SAMPLE_EMAILS = (
    "sam.rivera@example.com", "robin.ortiz@example.com", "alex.kim@example.com",
    "jordan.lee@example.com", "priya.shah@example.com", "morgan.diaz@example.com",
)
SAMPLE_TITLES = (
    "Functional tests for every application", "Rotate service account passwords",
    "INC0012345 follow-up: vendor patch", "Onboard tests to the new test platform",
)


def _t(name: str, *columns: str) -> sa.TableClause:
    return sa.table(name, *(sa.column(c) for c in columns))


person = _t("person", "id", "email", "active")
firedrill = _t("firedrill", "id", "public_id", "title", "created_by_id")
assignment = _t("assignment", "id", "public_id", "firedrill_id", "owner_id")
update = _t("update", "id", "assignment_id", "author_id", "on_behalf_of_id")
update_request = _t("update_request", "id", "assignment_id", "asked_by_id")
audit_log = _t("audit_log", "actor_id", "object_type", "object_id")
usage_event = _t("usage_event", "person_id")


def upgrade() -> None:
    op.add_column('person', sa.Column('password_hash', sa.Text(), nullable=True))
    _remove_sample_data(op.get_bind())


def _remove_sample_data(db: sa.Connection) -> None:
    people = set(db.scalars(sa.select(person.c.id).where(person.c.email.in_(SAMPLE_EMAILS))))
    if not people:
        return

    drills = db.execute(
        sa.select(firedrill.c.id, firedrill.c.public_id)
        .where(firedrill.c.title.in_(SAMPLE_TITLES), firedrill.c.created_by_id.in_(people))
    ).all()
    drill_ids = [d.id for d in drills]
    lines = db.execute(
        sa.select(assignment.c.id, assignment.c.public_id).where(assignment.c.firedrill_id.in_(drill_ids))
    ).all()
    line_ids = [a.id for a in lines]
    asks = list(db.scalars(sa.select(update_request.c.id).where(update_request.c.assignment_id.in_(line_ids))))

    db.execute(sa.delete(audit_log).where(sa.or_(
        sa.and_(audit_log.c.object_type == "firedrill", audit_log.c.object_id.in_([str(d.public_id) for d in drills])),
        sa.and_(audit_log.c.object_type == "assignment", audit_log.c.object_id.in_([str(a.public_id) for a in lines])),
        sa.and_(audit_log.c.object_type == "update_request", audit_log.c.object_id.in_([str(i) for i in asks])),
    )))
    db.execute(sa.delete(update_request).where(update_request.c.id.in_(asks)))
    db.execute(sa.delete(update).where(update.c.assignment_id.in_(line_ids)))
    db.execute(sa.delete(assignment).where(assignment.c.id.in_(line_ids)))
    db.execute(sa.delete(firedrill).where(firedrill.c.id.in_(drill_ids)))

    still_used: set[int] = set()
    for column in (firedrill.c.created_by_id, assignment.c.owner_id, update.c.author_id,
                   update.c.on_behalf_of_id, update_request.c.asked_by_id):
        still_used.update(db.scalars(sa.select(column).where(column.in_(people)).distinct()))
    gone = people - still_used

    db.execute(sa.update(person).where(person.c.id.in_(still_used)).values(active=False))
    db.execute(sa.delete(usage_event).where(usage_event.c.person_id.in_(gone)))
    db.execute(sa.delete(audit_log).where(
        audit_log.c.object_type == "person", audit_log.c.object_id.in_([str(i) for i in gone])))
    db.execute(sa.update(audit_log).where(audit_log.c.actor_id.in_(gone)).values(actor_id=None))
    db.execute(sa.delete(person).where(person.c.id.in_(gone)))


def downgrade() -> None:
    op.drop_column('person', 'password_hash')
