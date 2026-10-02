from datetime import date
from decimal import Decimal

import pytest

from app.core.exceptions import ValidationError
from app.domain.leave.rules import calculate_working_days


def test_working_days_exclude_weekends_and_holidays():
    result = calculate_working_days(date(2026, 10, 2), date(2026, 10, 5), {date(2026, 10, 2)})
    assert result == Decimal("1")


def test_weekend_only_range_has_zero_working_days():
    assert calculate_working_days(date(2026, 10, 3), date(2026, 10, 4), set()) == Decimal("0")


def test_single_working_day_is_inclusive():
    assert calculate_working_days(date(2026, 10, 5), date(2026, 10, 5), set()) == Decimal("1")


def test_invalid_date_range_is_rejected():
    with pytest.raises(ValidationError, match="End date"):
        calculate_working_days(date(2026, 10, 6), date(2026, 10, 5), set())

