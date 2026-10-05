"""Deterministic resolution of natural-language leave dates.

The model passes the employee's date phrase as written ("Tuesday and Sunday", "5th to 7th
October", "3 days from 12 Oct"); this module turns it into concrete calendar dates and says
whether they are separate days or one continuous range. Calendar arithmetic never comes from
the model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from app.core.exceptions import ValidationError

DateShape = Literal["single", "separate", "range", "none"]

MAX_RESOLVED_DAYS = 62

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
_ORD = r"(?:st|nd|rd|th)?"
_TOMORROW = r"(?:tomorrow|tomorow|tommorow|tommorrow)"
_RANGE_CONNECTOR = re.compile(r"^\s*(?:to|till|until|through|thru|upto|up to|-|–|—)\s*$")
_LIST_CONNECTOR = re.compile(r"^\s*(?:,|and|&|,\s*and|or)\s*$")


@dataclass(frozen=True, slots=True)
class _Mention:
    start: int
    end: int
    value: date


@dataclass(frozen=True, slots=True)
class DateResolution:
    shape: DateShape
    dates: tuple[date, ...] = field(default_factory=tuple)
    question: str | None = None

    @property
    def ambiguous(self) -> bool:
        return self.shape == "none"


def resolve_leave_dates(
    text: str, today: date, holidays: set[date] | frozenset[date] = frozenset()
) -> DateResolution:
    """Resolve a date phrase into concrete dates.

    Separate days ("Tuesday and Sunday") stay separate; only an explicit range connector
    ("to", "till", "-", "between ... and ...") or a duration ("for 3 days") makes a range.
    """
    normalized = " ".join(text.casefold().split())
    mentions = _mentions(normalized, today)

    duration = _duration(normalized)
    if duration and mentions:
        start = mentions[0].value
        return _range(start, _add_working_days(start, duration, holidays))
    if duration and re.search(r"\bfrom (?:today|now)\b", normalized):
        return _range(today, _add_working_days(today, duration, holidays))

    if not mentions:
        if re.search(r"\bnext week\b", normalized):
            monday = today + timedelta(days=7 - today.weekday())
            return _range(monday, monday + timedelta(days=4))
        if re.search(r"\bthis week\b", normalized):
            monday = today - timedelta(days=today.weekday())
            return _range(max(today, monday), monday + timedelta(days=4))
        return DateResolution("none", question="Which date or dates do you want?")

    if len(mentions) == 1:
        return DateResolution("single", (mentions[0].value,))

    between = bool(re.search(r"\bbetween\b", normalized[: mentions[0].start]))
    if len(mentions) == 2:
        gap = normalized[mentions[0].end : mentions[1].start]
        if _RANGE_CONNECTOR.match(gap) or (between and re.match(r"^\s*and\s*$", gap)):
            return _range(mentions[0].value, mentions[1].value)

    values = sorted(set(item.value for item in mentions))
    if len(values) == 1:
        return DateResolution("single", (values[0],))
    return DateResolution("separate", tuple(values))


def _range(start: date, end: date) -> DateResolution:
    if end < start:
        raise ValidationError("End date must be on or after start date")
    days = (end - start).days + 1
    if days > MAX_RESOLVED_DAYS:
        raise ValidationError(f"A leave request can cover at most {MAX_RESOLVED_DAYS} calendar days")
    if days == 1:
        return DateResolution("single", (start,))
    return DateResolution("range", tuple(start + timedelta(days=offset) for offset in range(days)))


def _duration(normalized: str) -> int | None:
    match = re.search(r"\b(?:for\s+)?(\d{1,2})\s+(?:working\s+)?days?\b", normalized)
    if not match or not re.search(r"\b(?:for|from|starting)\b", normalized):
        return None
    value = int(match.group(1))
    return value if value >= 1 else None


def _add_working_days(start: date, count: int, holidays: set[date] | frozenset[date]) -> date:
    current = start
    remaining = count - (1 if current.weekday() < 5 and current not in holidays else 0)
    while remaining > 0:
        current += timedelta(days=1)
        if current.weekday() < 5 and current not in holidays:
            remaining -= 1
    return current


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _mentions(normalized: str, today: date) -> list[_Mention]:
    found: list[_Mention] = []

    def add(start: int, end: int, value: date | None) -> None:
        if value is None:
            return
        if any(start < item.end and end > item.start for item in found):
            return
        found.append(_Mention(start, end, value))

    # "5th to 7th October" / "5-7 Oct": the month belongs to both numbers.
    for match in re.finditer(
        rf"\b(\d{{1,2}}){_ORD}\s*(to|till|until|through|-|–)\s*(\d{{1,2}}){_ORD}\s+({_MONTH})(?:\s+(\d{{4}}))?\b",
        normalized,
    ):
        month, year = _MONTHS[match.group(4)], int(match.group(5) or today.year)
        first_end = match.start(2)
        add(match.start(1), first_end, _safe_date(year, month, int(match.group(1))))
        add(match.start(3), match.end(), _safe_date(year, month, int(match.group(3))))

    # "6th, 8th and 9th October": a list of days sharing one month.
    for match in re.finditer(
        rf"\b((?:\d{{1,2}}{_ORD}\s*(?:,|and|&)\s*)+\d{{1,2}}{_ORD})\s+({_MONTH})(?:\s+(\d{{4}}))?\b",
        normalized,
    ):
        month, year = _MONTHS[match.group(2)], int(match.group(3) or today.year)
        for number in re.finditer(r"\d{1,2}", match.group(1)):
            start = match.start(1) + number.start()
            add(start, start + len(number.group(0)), _safe_date(year, month, int(number.group(0))))

    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", normalized):
        add(match.start(), match.end(), _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in re.finditer(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})(?:,?\s+(\d{{4}}))?\b", normalized):
        add(match.start(), match.end(), _safe_date(
            int(match.group(3) or today.year), _MONTHS[match.group(2)], int(match.group(1))
        ))
    for match in re.finditer(rf"\b({_MONTH})\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?\b", normalized):
        add(match.start(), match.end(), _safe_date(
            int(match.group(3) or today.year), _MONTHS[match.group(1)], int(match.group(2))
        ))
    for match in re.finditer(r"(?<![\d-])\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", normalized):
        year = int(match.group(3) or today.year)
        add(match.start(), match.end(), _safe_date(year + 2000 if year < 100 else year,
                                                   int(match.group(2)), int(match.group(1))))

    for match in re.finditer(rf"\bday after {_TOMORROW}\b", normalized):
        add(match.start(), match.end(), today + timedelta(days=2))
    for match in re.finditer(rf"\b{_TOMORROW}\b", normalized):
        add(match.start(), match.end(), today + timedelta(days=1))
    for match in re.finditer(r"\btoday\b", normalized):
        add(match.start(), match.end(), today)

    weekday_matches = list(re.finditer(
        r"\b(next\s+|this\s+|coming\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        normalized,
    ))
    previous: date | None = None
    for match in weekday_matches:
        target = _WEEKDAYS[match.group(2)]
        modifier = (match.group(1) or "").strip()
        if previous is None:
            delta = (target - today.weekday()) % 7
            if delta == 0 and modifier in {"next", "coming"}:
                delta = 7
            candidate = today + timedelta(days=delta)
        else:
            delta = (target - previous.weekday()) % 7 or 7
            candidate = previous + timedelta(days=delta)
        previous = candidate
        add(match.start(), match.end(), candidate)

    return sorted(found, key=lambda item: item.start)


def describe_day(value: date, holidays: set[date] | frozenset[date] = frozenset()) -> dict[str, object]:
    return {
        "date": value.isoformat(),
        "weekday": value.strftime("%A"),
        "is_weekend": value.weekday() >= 5,
        "is_holiday": value in holidays,
    }
