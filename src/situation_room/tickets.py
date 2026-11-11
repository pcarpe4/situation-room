"""Ticket references such as INC0012345. Longest prefix first, so SCTASK is never read as TASK."""

from __future__ import annotations

import re

PREFIXES = ("SCTASK", "CTASK", "RITM", "TASK", "INC", "CHG", "REQ", "PRB")
_PATTERN = re.compile(r"\b(" + "|".join(PREFIXES) + r")(\d{4,})\b", re.IGNORECASE)


def find_refs(text: str) -> list[str]:
    """Distinct refs in order of first appearance, uppercased."""
    seen: dict[str, None] = {}
    for match in _PATTERN.finditer(text or ""):
        seen.setdefault(match.group(1).upper() + match.group(2), None)
    return list(seen)
