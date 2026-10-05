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



@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("apply leave next week wednesday", ["2026-10-14"]),
        ("wednesday next week", ["2026-10-14"]),
        ("next week Monday and Tuesday", ["2026-10-12", "2026-10-13"]),
        ("this week friday", ["2026-10-09"]),
        ("next wednesday", ["2026-10-07"]),  # bare "next <day>" = the upcoming one
    ],
)
def test_weekdays_with_an_explicit_week(text, expected):
    assert iso(resolve_leave_dates(text, MONDAY)) == expected



@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15th next month", ["2026-11-15"]),
        ("next month 15th", ["2026-11-15"]),
        ("15th of next month", ["2026-11-15"]),
        ("first monday of next month", ["2026-11-02"]),
        ("next month first monday", ["2026-11-02"]),
        ("last friday of november", ["2026-11-27"]),
        ("after 2 weeks", ["2026-10-19"]),
        ("two weeks from now", ["2026-10-19"]),
        ("in a week", ["2026-10-12"]),
        ("jan 5", ["2027-01-05"]),  # day/month already past this year -> next year
        ("30th september", ["2026-09-30"]),  # within the 7-day back-dating window stays this year
        ("2026-01-05", ["2026-01-05"]),  # an explicit year is never changed
    ],
)
def test_month_relative_and_year_rollover_dates(text, expected):
    assert iso(resolve_leave_dates(text, MONDAY)) == expected


def test_range_across_the_new_year_rolls_the_end_date():
    result = resolve_leave_dates("from 23 dec to 2 jan", MONDAY)

    assert result.shape == "range"
    assert (iso(result)[0], iso(result)[-1]) == ("2026-12-23", "2027-01-02")


@pytest.mark.parametrize(
    ("text", "question"),
    [
        ("leave next month", "November 2026"),
        ("in november", "November 2026"),
        ("after two months", "December 2026"),
        ("first week of november", "November 2026"),
        ("end of this month", "October 2026"),
        ("sometime in march", "March 2027"),
    ],
)
def test_a_month_without_a_day_asks_which_day(text, question):
    result = resolve_leave_dates(text, MONDAY)

    assert result.ambiguous
    assert question in result.question



@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Can I take casual leave on Saturday 2026-11-07?", ["2026-11-07"]),
        ("on Wednesday, 18 Nov", ["2026-11-18"]),
        ("12 Oct (Monday)", ["2026-10-12"]),
    ],
)
def test_a_weekday_next_to_an_explicit_date_only_labels_it(text, expected):
    assert iso(resolve_leave_dates(text, MONDAY)) == expected


def test_weekday_labelled_range_stays_one_range():
    result = resolve_leave_dates("Monday 12 Oct to Friday 16 Oct", MONDAY)

    assert result.shape == "range"
    assert (iso(result)[0], iso(result)[-1]) == ("2026-10-12", "2026-10-16")


def test_between_two_ordinal_days_of_one_month_is_a_range():
    resolution = resolve_leave_dates("between 20th and 31st December", date(2026, 10, 6))

    assert resolution.shape == "range"
    assert resolution.dates[0] == date(2026, 12, 20) and resolution.dates[-1] == date(2026, 12, 31)


def test_a_list_of_ordinal_days_is_still_separate_days():
    resolution = resolve_leave_dates("6th, 8th and 9th November", date(2026, 10, 6))

    assert resolution.shape == "separate"
