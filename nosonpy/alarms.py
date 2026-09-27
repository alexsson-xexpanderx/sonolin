"""Alarm recurrence codes, shared by the GUI and the CLI (and free of Qt)."""

from __future__ import annotations

RECURRENCES = (
    ("DAILY", "Every day"),
    ("WEEKDAYS", "Weekdays"),
    ("WEEKENDS", "Weekends"),
    ("ONCE", "Once"),
)

DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def describe_recurrence(code: str) -> str:
    for key, label in RECURRENCES:
        if code == key:
            return label
    if code.startswith("ON_"):
        return ", ".join(DAY_NAMES[int(d)] for d in code[3:] if d.isdigit() and int(d) < 7)
    return code
