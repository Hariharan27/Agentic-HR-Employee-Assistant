"""Company holiday calendars by region (source: the 2026 holiday list PDFs in policy_docs)."""

from __future__ import annotations

from datetime import date

TAMIL_NADU = "TAMIL_NADU"
KARNATAKA = "KARNATAKA"
DEFAULT_REGION = TAMIL_NADU  # head office: Chennai

_KARNATAKA_LOCATIONS = {"bengaluru", "bangalore", "karnataka", "mysuru", "mysore"}


def region_for_location(location: str | None) -> str:
    """Map an employee location to the holiday calendar that applies to it."""
    normalized = (location or "").strip().casefold()
    if any(name in normalized for name in _KARNATAKA_LOCATIONS):
        return KARNATAKA
    return DEFAULT_REGION


# "Holiday List - 2026.pdf" (Chennai / Tamil Nadu)
_TAMIL_NADU_2026 = (
    (date(2026, 1, 1), "New Year"),
    (date(2026, 1, 15), "Pongal"),
    (date(2026, 1, 26), "Republic Day"),
    (date(2026, 3, 21), "Ramzan"),
    (date(2026, 4, 14), "Tamil New Year"),
    (date(2026, 5, 1), "May Day"),
    (date(2026, 8, 15), "Independence Day"),
    (date(2026, 9, 14), "Vinayakar Chathurthi"),
    (date(2026, 10, 2), "Gandhi Jayanthi"),
    (date(2026, 10, 19), "Ayudha Poojai"),
    (date(2026, 11, 8), "Diwali"),
    (date(2026, 12, 25), "Christmas"),
)

# "Karnataka Holiday List - 2026.pdf"
_KARNATAKA_2026 = (
    (date(2026, 1, 1), "New Year"),
    (date(2026, 1, 15), "Makara Sankranti"),
    (date(2026, 1, 26), "Republic Day"),
    (date(2026, 3, 19), "Ugadi"),
    (date(2026, 3, 21), "Ramzan"),
    (date(2026, 5, 1), "Labour Day"),
    (date(2026, 8, 15), "Independence Day"),
    (date(2026, 9, 14), "Vinayaka Chaturthi"),
    (date(2026, 10, 2), "Gandhi Jayanthi"),
    (date(2026, 11, 1), "Kannada Rajyothsava"),
    (date(2026, 11, 10), "Diwali"),
    (date(2026, 12, 25), "Christmas"),
)

HOLIDAY_CALENDARS: dict[str, tuple[tuple[date, str], ...]] = {
    TAMIL_NADU: _TAMIL_NADU_2026,
    KARNATAKA: _KARNATAKA_2026,
}
