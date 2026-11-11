"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-11-11 16:40:18.041699
"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('person',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('display_name', sa.String(length=200), nullable=False),
    sa.Column('email', sa.String(length=320), nullable=False),
    sa.Column('login_id', sa.String(length=320), nullable=False),
    sa.Column('role', sa.Enum('lead', 'manager', 'triage', name='role', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint('email = lower(email)', name=op.f('ck_person_email_lowercase')),
    sa.CheckConstraint('login_id = lower(login_id)', name=op.f('ck_person_login_id_lowercase')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_person')),
    sa.UniqueConstraint('email', name=op.f('uq_person_email')),
    sa.UniqueConstraint('login_id', name=op.f('uq_person_login_id'))
    )
    op.create_table('audit_log',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('actor_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=50), nullable=False),
    sa.Column('object_type', sa.String(length=30), nullable=False),
    sa.Column('object_id', sa.String(length=64), nullable=False),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_id'], ['person.id'], name=op.f('fk_audit_log_actor_id_person')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_log'))
    )
    op.create_table('firedrill',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=200), nullable=False),
    sa.Column('short_description', sa.String(length=500), nullable=False),
    sa.Column('state', sa.Enum('open', 'closed', name='firedrill_state', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('due_date', sa.Date(), nullable=True),
    sa.Column('requested_by', sa.String(length=200), nullable=True),
    sa.Column('source', sa.String(length=200), nullable=True),
    sa.Column('ticket_refs', sa.String(length=500), nullable=False),
    sa.Column('auto_ask', sa.Boolean(), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['created_by_id'], ['person.id'], name=op.f('fk_firedrill_created_by_id_person')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_firedrill')),
    sa.UniqueConstraint('public_id', name=op.f('uq_firedrill_public_id'))
    )
    op.create_table('usage_event',
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('day', sa.Date(), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['person.id'], name=op.f('fk_usage_event_person_id_person')),
    sa.PrimaryKeyConstraint('person_id', 'day', 'kind', name=op.f('pk_usage_event'))
    )
    op.create_table('assignment',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.Uuid(), nullable=False),
    sa.Column('firedrill_id', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.Enum('new', 'on_track', 'at_risk', 'blocked', 'done', name='status', native_enum=False, create_constraint=True, length=20), nullable=False),
    sa.Column('waiting_until', sa.Date(), nullable=True),
    sa.Column('waiting_reason', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ended_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['firedrill_id'], ['firedrill.id'], name=op.f('fk_assignment_firedrill_id_firedrill')),
    sa.ForeignKeyConstraint(['owner_id'], ['person.id'], name=op.f('fk_assignment_owner_id_person')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_assignment')),
    sa.UniqueConstraint('public_id', name=op.f('uq_assignment_public_id'))
    )
    op.create_index('ix_assignment_owner_unended', 'assignment', ['owner_id'], unique=False, postgresql_where=sa.text('ended_at IS NULL'))
    op.create_index('uq_assignment_one_unended_per_owner', 'assignment', ['firedrill_id', 'owner_id'], unique=True, postgresql_where=sa.text('ended_at IS NULL'))
    op.create_table('update',
    sa.Column('id', sa.BigInteger(), nullable=False),
    sa.Column('assignment_id', sa.Integer(), nullable=False),
    sa.Column('author_id', sa.Integer(), nullable=False),
    sa.Column('on_behalf_of_id', sa.Integer(), nullable=True),
    sa.Column('status', sa.Enum('new', 'on_track', 'at_risk', 'blocked', 'done', name='update_status', native_enum=False, create_constraint=True, length=20), nullable=True),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('source', sa.String(length=20), nullable=False),
    sa.Column('form_token', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['assignment_id'], ['assignment.id'], name=op.f('fk_update_assignment_id_assignment')),
    sa.ForeignKeyConstraint(['author_id'], ['person.id'], name=op.f('fk_update_author_id_person')),
    sa.ForeignKeyConstraint(['on_behalf_of_id'], ['person.id'], name=op.f('fk_update_on_behalf_of_id_person')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_update')),
    sa.UniqueConstraint('form_token', name=op.f('uq_update_form_token'))
    )
    op.create_index('ix_update_assignment_newest', 'update', ['assignment_id', 'id'], unique=False)
    op.create_table('update_request',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('assignment_id', sa.Integer(), nullable=False),
    sa.Column('asked_by_id', sa.Integer(), nullable=False),
    sa.Column('origin', sa.String(length=10), nullable=False),
    sa.Column('requested_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confirmed_sent_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancelled_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('cancel_reason', sa.String(length=30), nullable=True),
    sa.Column('answered_by_update_id', sa.BigInteger(), nullable=True),
    sa.Column('batch_id', sa.Uuid(), nullable=True),
    sa.ForeignKeyConstraint(['answered_by_update_id'], ['update.id'], name=op.f('fk_update_request_answered_by_update_id_update')),
    sa.ForeignKeyConstraint(['asked_by_id'], ['person.id'], name=op.f('fk_update_request_asked_by_id_person')),
    sa.ForeignKeyConstraint(['assignment_id'], ['assignment.id'], name=op.f('fk_update_request_assignment_id_assignment')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_update_request'))
    )
    op.create_index('ix_update_request_open', 'update_request', ['assignment_id'], unique=False, postgresql_where=sa.text('answered_by_update_id IS NULL AND cancelled_at IS NULL'))


def downgrade() -> None:
    op.drop_index('ix_update_request_open', table_name='update_request', postgresql_where=sa.text('answered_by_update_id IS NULL AND cancelled_at IS NULL'))
    op.drop_table('update_request')
    op.drop_index('ix_update_assignment_newest', table_name='update')
    op.drop_table('update')
    op.drop_index('uq_assignment_one_unended_per_owner', table_name='assignment', postgresql_where=sa.text('ended_at IS NULL'))
    op.drop_index('ix_assignment_owner_unended', table_name='assignment', postgresql_where=sa.text('ended_at IS NULL'))
    op.drop_table('assignment')
    op.drop_table('usage_event')
    op.drop_table('firedrill')
    op.drop_table('audit_log')
    op.drop_table('person')
