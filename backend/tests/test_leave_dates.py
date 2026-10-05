from datetime import date

import pytest

from app.core.exceptions import ValidationError
from app.domain.leave.dates import resolve_leave_dates

MONDAY = date(2026, 10, 5)
HOLIDAYS = {date(2026, 10, 7)}


def iso(resolution):
    return [item.isoformat() for item in resolution.dates]


def test_two_weekdays_joined_by_and_are_separate_days_not_a_range():
    result = resolve_leave_dates("Can I take casual leave on Tuesday and Sunday", MONDAY)

    assert result.shape == "separate"
    assert iso(result) == ["2026-10-06", "2026-10-11"]


def test_weekdays_joined_by_to_are_a_range():
    result = resolve_leave_dates("casual leave from Tuesday to Sunday", MONDAY)

    assert result.shape == "range"
    assert iso(result)[0] == "2026-10-06" and iso(result)[-1] == "2026-10-11"
    assert len(result.dates) == 6


@pytest.mark.parametrize(
    ("text", "today", "expected"),
    [
        ("Apply casual leave on 5 October", date(2026, 10, 4), ["2026-10-05"]),
        ("2026-10-12", MONDAY, ["2026-10-12"]),
        ("tomorrow", date(2026, 10, 4), ["2026-10-05"]),
        ("sick leave tommorrow", date(2026, 10, 4), ["2026-10-05"]),
        ("day after tomorrow", MONDAY, ["2026-10-07"]),
        ("next Monday", MONDAY, ["2026-10-12"]),
        ("Apply casual leave October 12", MONDAY, ["2026-10-12"]),
    ],
)
def test_single_dates(text, today, expected):
    result = resolve_leave_dates(text, today)

    assert result.shape == "single"
    assert iso(result) == expected


@pytest.mark.parametrize(
    ("text", "today", "start", "end"),
    [
        ("Apply casual leave from 5th to 7th October", MONDAY, "2026-10-05", "2026-10-07"),
        ("create leave from 13th october to 14th october", date(2026, 10, 4), "2026-10-13", "2026-10-14"),
        ("between 12 Oct and 14 Oct", MONDAY, "2026-10-12", "2026-10-14"),
        ("Apply casual leave for 5 days from today", MONDAY, "2026-10-05", "2026-10-09"),
        ("Apply earned leave from 5 October for 5 days", date(2026, 10, 4), "2026-10-05", "2026-10-09"),
        ("raise privilege leave request from 5 October for 2 days", date(2026, 10, 4), "2026-10-05", "2026-10-06"),
        ("Apply casual leave from next Monday for 3 working days", MONDAY, "2026-10-12", "2026-10-14"),
        ("Apply casual leave from 9 October for 3 working days", MONDAY, "2026-10-09", "2026-10-13"),
        ("next week", MONDAY, "2026-10-12", "2026-10-16"),
    ],
)
def test_ranges_and_durations(text, today, start, end):
    result = resolve_leave_dates(text, today)

    assert result.shape == "range"
    assert iso(result)[0] == start
    assert iso(result)[-1] == end


def test_duration_skips_configured_holidays():
    result = resolve_leave_dates("for 3 working days from today", MONDAY, HOLIDAYS)

    assert iso(result)[-1] == "2026-10-08"  # Mon, Tue, (Wed holiday), Thu


def test_list_of_days_sharing_one_month_is_separate():
    result = resolve_leave_dates("6th, 8th and 9th October", MONDAY)

    assert result.shape == "separate"
    assert iso(result) == ["2026-10-06", "2026-10-08", "2026-10-09"]


def test_no_dates_is_ambiguous_with_a_question():
    result = resolve_leave_dates("I want to take leave", MONDAY)

    assert result.ambiguous
    assert result.question


def test_reversed_range_is_rejected():
    with pytest.raises(ValidationError, match="End date must be on or after start date"):
        resolve_leave_dates("from 20 October to 10 October", MONDAY)
