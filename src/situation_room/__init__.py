"""Situation Room: a firedrill board.

The rule the whole app follows: everything is an event on an assignment.
A firedrill has one assignment per owner, and updates, asks and waits all
belong to an assignment. Only `situation_room.domain` changes assignments.
"""
