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
    text: str, today: date, holidays: dict[date, str] | set[date] | frozenset[date] = frozenset()
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
        return DateResolution(
            "none", question=_vague_month_question(normalized, today) or "Which date or dates do you want?"
        )

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


_NUMBER_WORDS = {"one": 1, "a": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
_ORDINALS = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": -1}
BACKDATE_GRACE_DAYS = 7  # a day/month without a year further back than this means next year


def _implicit_date(today: date, month: int, day: int, year: str | None) -> date | None:
    """A date whose year was not written: this year, or next year once it is clearly past."""
    if year:
        return _safe_date(int(year) + (2000 if len(year) == 2 else 0), month, day)
    value = _safe_date(today.year, month, day)
    if value is not None and value < today - timedelta(days=BACKDATE_GRACE_DAYS):
        value = _safe_date(today.year + 1, month, day)
    return value


def _add_months(value: date, months: int) -> tuple[int, int]:
    index = value.year * 12 + value.month - 1 + months
    return index // 12, index % 12 + 1


def _nth_weekday(year: int, month: int, weekday: int, nth: int) -> date | None:
    if nth == -1:
        last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        return last - timedelta(days=(last.weekday() - weekday) % 7)
    first = date(year, month, 1)
    candidate = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (nth - 1))
    return candidate if candidate.month == month else None


def _vague_month_question(normalized: str, today: date) -> str | None:
    """A month was named without a day: ask for the day(s) in that month instead of guessing."""
    target: tuple[int, int] | None = None
    match = re.search(r"\b(?:in|after)\s+(\d+|one|two|three|four|five|six)\s+months?\b", normalized)
    if match:
        count = _NUMBER_WORDS.get(match.group(1)) or int(match.group(1))
        target = _add_months(today, count)
    elif re.search(r"\bnext month\b", normalized):
        target = _add_months(today, 1)
    elif re.search(r"\b(?:this month|end of (?:the|this) month)\b", normalized):
        target = (today.year, today.month)
    else:
        month = re.search(rf"\b({_MONTH})\b(?:\s+(\d{{4}}))?", normalized)
        if month:
            number = _MONTHS[month.group(1)]
            year = int(month.group(2)) if month.group(2) else today.year + (number < today.month)
            target = (year, number)
    if target is None:
        return None
    label = date(target[0], target[1], 1).strftime("%B %Y")
    return f"Which day or days in {label}?"


def _mentions(normalized: str, today: date) -> list[_Mention]:
    found: list[_Mention] = []

    def add(start: int, end: int, value: date | None) -> bool:
        if value is None:
            return False
        if any(start < item.end and end > item.start for item in found):
            return False
        found.append(_Mention(start, end, value))
        return True

    # "first Monday of next month", "last Friday of November", "next month first monday"
    for match in re.finditer(
        rf"\b(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
        rf"\s+(?:of\s+|in\s+)?(next month|this month|{_MONTH})\b",
        normalized,
    ):
        if match.group(3) == "next month":
            year, month = _add_months(today, 1)
        elif match.group(3) == "this month":
            year, month = today.year, today.month
        else:
            month = _MONTHS[match.group(3)]
            year = today.year + (month < today.month)
        add(match.start(), match.end(), _nth_weekday(year, month, _WEEKDAYS[match.group(2)], _ORDINALS[match.group(1)]))
    for match in re.finditer(
        r"\b(next month|this month)\s+(?:the\s+)?(first|1st|second|2nd|third|3rd|fourth|4th|last)\s+"
        r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        normalized,
    ):
        year, month = _add_months(today, 1) if match.group(1) == "next month" else (today.year, today.month)
        add(match.start(), match.end(), _nth_weekday(year, month, _WEEKDAYS[match.group(3)], _ORDINALS[match.group(2)]))

    # "15th next month", "15th of next month", "next month 15th"
    for match in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?(next|this) month\b", normalized):
        year, month = _add_months(today, 1) if match.group(2) == "next" else (today.year, today.month)
        add(match.start(), match.end(), _safe_date(year, month, int(match.group(1))))
    for match in re.finditer(r"\b(next|this) month(?:'s)?\s+(?:on\s+)?(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?\b", normalized):
        year, month = _add_months(today, 1) if match.group(1) == "next" else (today.year, today.month)
        add(match.start(), match.end(), _safe_date(year, month, int(match.group(2))))

    # "after 2 weeks", "in two weeks", "two weeks from now"
    for match in re.finditer(
        r"\b(?:(?:in|after)\s+(\d+|one|a|two|three|four)\s+weeks?|(\d+|one|a|two|three|four)\s+weeks?\s+from\s+(?:now|today))\b",
        normalized,
    ):
        token = match.group(1) or match.group(2)
        count = _NUMBER_WORDS.get(token) or int(token)
        add(match.start(), match.end(), today + timedelta(days=7 * count))

    # "5th to 7th October" / "5-7 Oct": the month belongs to both numbers.
    for match in re.finditer(
        rf"\b(\d{{1,2}}){_ORD}\s*(to|till|until|through|-|–)\s*(\d{{1,2}}){_ORD}\s+({_MONTH})(?:\s+(\d{{4}}))?\b",
        normalized,
    ):
        month = _MONTHS[match.group(4)]
        first_end = match.start(2)
        add(match.start(1), first_end, _implicit_date(today, month, int(match.group(1)), match.group(5)))
        add(match.start(3), match.end(), _implicit_date(today, month, int(match.group(3)), match.group(5)))

    # "6th, 8th and 9th October": a list of days sharing one month.
    for match in re.finditer(
        rf"\b((?:\d{{1,2}}{_ORD}\s*(?:,|and|&)\s*)+\d{{1,2}}{_ORD})\s+({_MONTH})(?:\s+(\d{{4}}))?\b",
        normalized,
    ):
        month = _MONTHS[match.group(2)]
        for number in re.finditer(r"\d{1,2}", match.group(1)):
            start = match.start(1) + number.start()
            add(start, start + len(number.group(0)), _implicit_date(today, month, int(number.group(0)), match.group(3)))

    for match in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", normalized):
        add(match.start(), match.end(), _safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in re.finditer(rf"\b(\d{{1,2}}){_ORD}\s+(?:of\s+)?({_MONTH})(?:,?\s+(\d{{4}}))?\b", normalized):
        add(match.start(), match.end(), _implicit_date(today, _MONTHS[match.group(2)], int(match.group(1)), match.group(3)))
    for match in re.finditer(rf"\b({_MONTH})\s+(\d{{1,2}}){_ORD}(?:,?\s+(\d{{4}}))?\b", normalized):
        add(match.start(), match.end(), _implicit_date(today, _MONTHS[match.group(1)], int(match.group(2)), match.group(3)))
    for match in re.finditer(r"(?<![\d-])\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", normalized):
        add(match.start(), match.end(), _implicit_date(today, int(match.group(2)), int(match.group(1)), match.group(3)))

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
    # "next week Wednesday" / "Wednesday next week" name a day in the next calendar week (Mon-Sun);
    # "this week Friday" one in the current week. A bare "Wednesday" is the upcoming one.
    week_start: date | None = None
    if re.search(r"\bnext week\b", normalized):
        week_start = today - timedelta(days=today.weekday()) + timedelta(days=7)
    elif re.search(r"\bthis week\b", normalized):
        week_start = today - timedelta(days=today.weekday())
    previous: date | None = None
    for match in weekday_matches:
        target = _WEEKDAYS[match.group(2)]
        modifier = (match.group(1) or "").strip()
        if previous is None and week_start is not None:
            candidate = week_start + timedelta(days=target)
        elif previous is None:
            delta = (target - today.weekday()) % 7
            if delta == 0 and modifier in {"next", "coming"}:
                delta = 7
            candidate = today + timedelta(days=delta)
        else:
            delta = (target - previous.weekday()) % 7 or 7
            candidate = previous + timedelta(days=delta)
        if add(match.start(), match.end(), candidate):
            previous = candidate

    return sorted(found, key=lambda item: item.start)


def describe_day(value: date, holidays: dict[date, str] | set[date] | frozenset[date] = frozenset()) -> dict[str, object]:
    described: dict[str, object] = {
        "date": value.isoformat(),
        "weekday": value.strftime("%A"),
        "is_weekend": value.weekday() >= 5,
        "is_holiday": value in holidays,
    }
    if isinstance(holidays, dict) and value in holidays:
        described["holiday"] = holidays[value]
    return described
