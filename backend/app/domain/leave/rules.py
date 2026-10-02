from datetime import date, timedelta
from decimal import Decimal

from app.core.exceptions import ValidationError


def validate_date_range(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise ValidationError("End date must be on or after start date")


def calculate_working_days(start_date: date, end_date: date, holidays: set[date]) -> Decimal:
    validate_date_range(start_date, end_date)
    days = (end_date - start_date).days + 1
    working_days = sum(
        1
        for offset in range(days)
        if (current := start_date + timedelta(days=offset)).weekday() < 5 and current not in holidays
    )
    return Decimal(working_days)

