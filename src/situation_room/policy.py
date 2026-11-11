"""Who may do what. Deny by default; every handler asks here.

State rules (an ended assignment, a closed firedrill) live in the domain module.
This module answers only whether the person's role and ownership allow the action.
"""

from __future__ import annotations

import enum

from .models import Assignment, Person, Role


class Action(enum.StrEnum):
    VIEW_BOARD = "view_board"
    VIEW_FIREDRILL = "view_firedrill"
    CREATE_FIREDRILL = "create_firedrill"
    REQUEST_UPDATE = "request_update"
    POST_UPDATE = "post_update"
    MANAGE_ROSTER = "manage_roster"


LEAD_OR_TRIAGE = (Role.LEAD, Role.TRIAGE)


def can(person: Person | None, action: Action, obj: object | None = None) -> bool:
    if person is None or not person.active:
        return False
    match action:
        case Action.VIEW_BOARD | Action.VIEW_FIREDRILL:
            # Open Question 2 default: everyone signed in reads every firedrill.
            return True
        case Action.CREATE_FIREDRILL | Action.REQUEST_UPDATE:
            return person.role in LEAD_OR_TRIAGE
        case Action.POST_UPDATE:
            if not isinstance(obj, Assignment):
                return False
            return obj.owner_id == person.id or person.role in LEAD_OR_TRIAGE
        case Action.MANAGE_ROSTER:
            return person.role == Role.TRIAGE
    return False
