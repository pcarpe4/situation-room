"""What each viewer sees and may do on an owner line. No database, no HTML."""

from datetime import datetime, timezone

import pytest

from situation_room import board, domain
from situation_room.board import AskState as S
from situation_room.board import LineAction as A
from situation_room.board import OpenAsk, line_view
from situation_room.models import Assignment, Person, Role, Status

from .test_domain import assignment_of, make

T = datetime(2026, 11, 10, 15, tzinfo=timezone.utc)
LEAD, TRIAGE, ANN, BOB = 1, 2, 3, 4
PEOPLE = {
    "lead": Person(id=LEAD, role=Role.LEAD, active=True),
    "triage": Person(id=TRIAGE, role=Role.TRIAGE, active=True),
    "ann": Person(id=ANN, role=Role.MANAGER, active=True),
    "bob": Person(id=BOB, role=Role.MANAGER, active=True),
}


def ask(n, by, sent=False):
    return OpenAsk(id=n, requested_at=T, confirmed_sent_at=T if sent else None, asked_by_id=by)


CASES = [
    # viewer, owner, status, has_update, waiting, asks (newest first) -> state, ask id, actions, primary
    ("lead", ANN, Status.NEW, False, False, [], S.NONE, None, (A.REQUEST, A.POST_FOR), A.REQUEST),
    ("lead", ANN, Status.ON_TRACK, True, False, [], S.NONE, None, (A.REQUEST, A.POST_FOR), None),
    ("triage", ANN, Status.AT_RISK, True, False, [], S.NONE, None, (A.REQUEST, A.POST_FOR), None),
    ("lead", ANN, Status.NEW, False, False, [ask(1, LEAD)], S.MINE_UNSENT, 1, (A.CONFIRM_SENT, A.CANCEL_ASK, A.POST_FOR), None),
    ("lead", ANN, Status.NEW, False, False, [ask(2, TRIAGE), ask(1, LEAD)], S.MINE_UNSENT, 1,
     (A.CONFIRM_SENT, A.CANCEL_ASK, A.POST_FOR), None),
    ("lead", ANN, Status.NEW, False, False, [ask(2, TRIAGE)], S.OTHERS_UNSENT, 2, (A.ASK_AGAIN, A.POST_FOR), None),
    ("lead", ANN, Status.NEW, False, False, [ask(3, TRIAGE), ask(2, TRIAGE, sent=True)], S.SENT, 2,
     (A.ASK_AGAIN, A.POST_FOR), None),
    ("lead", ANN, Status.BLOCKED, True, False, [ask(2, TRIAGE, sent=True)], S.SENT, 2, (A.ASK_AGAIN, A.POST_FOR), None),
    ("lead", ANN, Status.ON_TRACK, True, True, [], S.WAITING, None, (A.ASK_ANYWAY, A.POST_FOR), None),
    ("lead", ANN, Status.ON_TRACK, True, True, [ask(2, TRIAGE, sent=True)], S.SENT, 2, (A.ASK_AGAIN, A.POST_FOR), None),
    ("ann", ANN, Status.ON_TRACK, True, False, [ask(2, LEAD, sent=True)], S.SENT, 2, (A.UPDATE,), A.UPDATE),
    ("ann", ANN, Status.NEW, False, False, [], S.NONE, None, (A.UPDATE,), A.UPDATE),
    ("bob", ANN, Status.BLOCKED, True, False, [ask(2, LEAD, sent=True)], S.SENT, 2, (), None),
    ("ann", ANN, Status.DONE, True, False, [], S.NONE, None, (A.UPDATE,), None),
    ("lead", ANN, Status.DONE, True, False, [], S.NONE, None, (), None),
    ("lead", LEAD, Status.NEW, False, False, [], S.NONE, None, (A.REQUEST, A.UPDATE), A.UPDATE),
]


@pytest.mark.parametrize(("viewer", "owner", "status", "has_update", "waiting", "asks",
                          "state", "ask_id", "actions", "primary"), CASES)
def test_line_view(viewer, owner, status, has_update, waiting, asks, state, ask_id, actions, primary):
    view = line_view(PEOPLE[viewer], Assignment(owner_id=owner, status=status),
                     has_update=has_update, waiting=waiting, asks=asks)
    assert view.ask_state == state
    assert (view.ask.id if view.ask else None) == ask_id
    assert view.actions == actions
    assert view.primary == primary


@pytest.mark.parametrize("owner", [ANN, LEAD])
def test_an_inactive_viewer_gets_no_actions_even_on_their_own_line(owner):
    gone = Person(id=owner, role=Role.LEAD if owner == LEAD else Role.MANAGER, active=False)
    for status in (Status.NEW, Status.DONE):
        assert line_view(gone, Assignment(owner_id=owner, status=status), has_update=False, waiting=False,
                         asks=[]).actions == ()


def test_asked_counts_only_asks_that_were_emailed(db, clock, people):
    a = assignment_of(db, make(db, clock, people), people.ann)
    unsent = domain.request_update(db, clock, people.lead, a.public_id)
    db.commit()
    assert not board.build(db, clock, 3, people.lead).rows[0].lines[0].asked
    domain.confirm_ask_sent(db, clock, people.lead, unsent.id)
    db.commit()
    assert board.build(db, clock, 3, people.lead).rows[0].lines[0].asked


def test_a_newer_ask_by_someone_else_does_not_hide_my_unsent_one(db, clock, people):
    a = assignment_of(db, make(db, clock, people), people.ann)
    mine = domain.request_update(db, clock, people.lead, a.public_id)
    domain.request_update(db, clock, people.triage, a.public_id)
    db.commit()
    line = board.build(db, clock, 3, people.lead).rows[0].lines[0]
    assert line.ask_state == S.MINE_UNSENT and line.ask.id == mine.id
    assert A.CONFIRM_SENT in line.actions
