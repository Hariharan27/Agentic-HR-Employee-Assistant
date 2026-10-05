"""Onboarding draft: the candidate fields collected over chat and the form, validated per field."""

from __future__ import annotations

import re
from datetime import date

from app.domain.leave.dates import resolve_leave_dates

FIELDS: tuple[str, ...] = (
    "name", "email", "designation", "department",
    "reporting_manager", "joining_date", "location", "employment_type",
)
LABELS: dict[str, str] = {
    "name": "employee name",
    "email": "email",
    "designation": "designation",
    "department": "department",
    "reporting_manager": "reporting manager",
    "joining_date": "joining date",
    "location": "location",
    "employment_type": "employment type",
}
EMPLOYMENT_TYPES = ("Permanent", "Contract", "Intern")
LOCATIONS = ("Chennai", "Bengaluru")
_EMPLOYMENT_ALIASES = {
    "permanent": "Permanent", "perm": "Permanent", "full time": "Permanent", "full-time": "Permanent",
    "fulltime": "Permanent", "regular": "Permanent",
    "contract": "Contract", "contractor": "Contract", "contractual": "Contract",
    "intern": "Intern", "internship": "Intern",
}
_LOCATION_ALIASES = {
    "chennai": "Chennai", "madras": "Chennai",
    "bengaluru": "Bengaluru", "bangalore": "Bengaluru", "blr": "Bengaluru",
}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z .'\-]{1,159}$")


def missing_fields(draft: dict[str, str]) -> list[str]:
    return [LABELS[field] for field in FIELDS if not draft.get(field)]


def normalize_employment_type(value: str) -> str | None:
    return _EMPLOYMENT_ALIASES.get(" ".join(value.casefold().split()))


def normalize_location(value: str) -> str | None:
    normalized = " ".join(value.casefold().split())
    return next((canonical for alias, canonical in _LOCATION_ALIASES.items() if alias in normalized), None)


def normalize_field(field: str, value: str, today: date) -> tuple[str | None, str | None]:
    """Return (normalized value, error). Reporting managers are resolved by the service."""
    text = " ".join(str(value).split())
    if not text:
        return None, f"{LABELS[field]} is empty"
    if field == "name":
        return (text, None) if _NAME_PATTERN.fullmatch(text) else (None, "a name uses letters, spaces, dots, hyphens or apostrophes")
    if field == "email":
        lowered = text.casefold()
        return (lowered, None) if EMAIL_PATTERN.fullmatch(lowered) else (None, "not a valid email address")
    if field in {"designation", "department", "reporting_manager"}:
        return (text, None) if 2 <= len(text) <= 120 else (None, f"{LABELS[field]} must be 2 to 120 characters")
    if field == "employment_type":
        result = normalize_employment_type(text)
        return (result, None) if result else (None, "employment type must be " + ", ".join(EMPLOYMENT_TYPES))
    if field == "location":
        result = normalize_location(text)
        return (result, None) if result else (None, "location must be " + " or ".join(LOCATIONS))
    if field == "joining_date":
        try:
            resolution = resolve_leave_dates(text, today)
        except Exception:  # noqa: BLE001 - invalid date words are a validation error here
            return None, "the joining date is not a valid date"
        if resolution.shape != "single":
            return None, resolution.question or "give one joining date"
        joining = resolution.dates[0]
        if joining < today:
            return None, f"the joining date must be today ({today.isoformat()}) or later"
        return joining.isoformat(), None
    return None, f"unknown field {field}"
