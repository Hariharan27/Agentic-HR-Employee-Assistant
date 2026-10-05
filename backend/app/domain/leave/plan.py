"""Leave plan: the single validated object an employee sees and later confirms."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal

from app.domain.leave.entities import LeaveType


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


@dataclass(frozen=True, slots=True)
class PlanSegment:
    leave_type: LeaveType
    start_date: date
    end_date: date
    working_days: Decimal


@dataclass(frozen=True, slots=True)
class ExcludedDay:
    day: date
    reason: str  # "weekly off", "holiday"
    name: str | None = None  # holiday name


@dataclass(frozen=True, slots=True)
class LeavePlan:
    leave_type: LeaveType
    requested_dates: tuple[date, ...]
    segments: tuple[PlanSegment, ...]
    excluded_days: tuple[ExcludedDay, ...]
    available_before: Decimal
    problems: tuple[str, ...]
    reason: str | None = None
    # A split the employee agreed to: remaining days use this second type.
    split_with: LeaveType | None = None
    secondary_available: Decimal | None = None
    # When the requested type is short: other types that could cover the shortfall.
    split_options: tuple[tuple[LeaveType, Decimal], ...] = ()
    plan_id: str = ""
    expires_at: datetime | None = None
    fingerprint: str = field(default="")

    @property
    def total_working_days(self) -> Decimal:
        return sum((item.working_days for item in self.segments), Decimal("0"))

    @property
    def eligible(self) -> bool:
        return not self.problems and bool(self.segments)

    def days_by_type(self) -> dict[LeaveType, Decimal]:
        totals: dict[LeaveType, Decimal] = {}
        for item in self.segments:
            totals[item.leave_type] = totals.get(item.leave_type, Decimal("0")) + item.working_days
        return totals

    @property
    def available_after(self) -> Decimal:
        return self.available_before - self.days_by_type().get(self.leave_type, Decimal("0"))

    def compute_fingerprint(self) -> str:
        payload = {
            "type": self.leave_type.value,
            "split_with": self.split_with.value if self.split_with else None,
            "segments": [
                [s.leave_type.value, s.start_date.isoformat(), s.end_date.isoformat(), _number(s.working_days)]
                for s in self.segments
            ],
            "excluded": [[e.day.isoformat(), e.reason] for e in self.excluded_days],
            "available": _number(self.available_before),
            "secondary_available": _number(self.secondary_available) if self.secondary_available is not None else None,
            "problems": list(self.problems),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    @staticmethod
    def _segment_text(segments: tuple[PlanSegment, ...] | list[PlanSegment]) -> str:
        parts = []
        for item in segments:
            if item.start_date == item.end_date:
                parts.append(f"{item.start_date.isoformat()} ({item.start_date.strftime('%a')})")
            else:
                parts.append(f"{item.start_date.isoformat()} to {item.end_date.isoformat()}")
        return "; ".join(parts)

    def segment_text(self) -> str:
        return self._segment_text(self.segments)

    def excluded_text(self) -> str:
        return ", ".join(
            f"{item.day.isoformat()} ({item.day.strftime('%a')}, "
            f"{item.reason + ': ' + item.name if item.name else item.reason})"
            for item in self.excluded_days
        )

    def _by_type_text(self) -> str:
        parts = []
        for kind, days in self.days_by_type().items():
            segments = [item for item in self.segments if item.leave_type is kind]
            parts.append(f"{_number(days)} {kind.value.title()} on {self._segment_text(segments)}")
        return " + ".join(parts)

    def summary(self) -> str:
        label = self.leave_type.value.title()
        if not self.segments:
            text = f"{label} leave: no working days in the requested dates."
        elif self.split_with is not None:
            text = f"Leave plan: {_number(self.total_working_days)} working day(s): {self._by_type_text()}."
        else:
            text = f"{label} leave: {_number(self.total_working_days)} working day(s) on {self.segment_text()}."
        if self.excluded_days:
            text += f" Not counted: {self.excluded_text()}."
        if self.eligible:
            text += (
                f" You are eligible. {label} balance {_number(self.available_before)} available, "
                f"{_number(self.available_after)} after this leave."
            )
            if self.split_with is not None and self.secondary_available is not None:
                used = self.days_by_type().get(self.split_with, Decimal("0"))
                text += (
                    f" {self.split_with.value.title()} balance {_number(self.secondary_available)} available, "
                    f"{_number(self.secondary_available - used)} after this leave."
                )
        else:
            text += " You are not eligible: " + "; ".join(self.problems) + "."
            if self.split_options:
                text += " You could cover the rest with " + " or ".join(
                    f"{kind.value.title()} leave ({_number(days)} available)" for kind, days in self.split_options
                ) + "."
        return text

    def confirmation_summary(self) -> str:
        label = self.leave_type.value.title()
        days = _number(self.total_working_days)
        if self.split_with is not None:
            text = f"Apply for {days} working day(s): {self._by_type_text()}"
        elif len(self.segments) == 1:
            segment = self.segments[0]
            text = (
                f"Apply for {days} working day(s) of {label} leave from "
                f"{segment.start_date.isoformat()} to {segment.end_date.isoformat()}"
            )
        else:
            text = f"Apply for {days} working day(s) of {label} leave on {self.segment_text()}"
        if self.excluded_days:
            text += f" (not counted: {self.excluded_text()})"
        return text

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "leave_type": self.leave_type.value,
            "split_with": self.split_with.value if self.split_with else None,
            "requested_dates": [item.isoformat() for item in self.requested_dates],
            "segments": [
                {
                    "leave_type": item.leave_type.value,
                    "start_date": item.start_date.isoformat(),
                    "end_date": item.end_date.isoformat(),
                    "working_days": _number(item.working_days),
                }
                for item in self.segments
            ],
            "days_by_type": {kind.value: _number(days) for kind, days in self.days_by_type().items()},
            "excluded_days": [
                {"date": item.day.isoformat(), "reason": item.reason, **({"holiday": item.name} if item.name else {})}
                for item in self.excluded_days
            ],
            "total_working_days": _number(self.total_working_days),
            "available_before": _number(self.available_before),
            "available_after": _number(self.available_after),
            "eligible": self.eligible,
            "problems": list(self.problems),
            "split_options": [
                {"leave_type": kind.value, "available": _number(days)} for kind, days in self.split_options
            ],
            "reason": self.reason,
            "summary": self.summary(),
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def plan_ttl() -> timedelta:
    return timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class StoredPlan:
    """What is needed to rebuild a plan persisted on the conversation."""

    plan_id: str
    leave_type: str
    dates: tuple[date, ...]
    reason: str | None
    split_with: str | None
    fingerprint: str
    expires_at: datetime


def stored_plan_inputs(value: object) -> StoredPlan | None:
    """Validate a plan persisted on the conversation."""
    if not isinstance(value, dict):
        return None
    try:
        plan_id = str(value["plan_id"])
        leave_type = LeaveType.parse(str(value["leave_type"])).value
        dates = tuple(sorted({date.fromisoformat(str(item)) for item in value["requested_dates"]}))
        fingerprint = str(value["fingerprint"])
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
        split_raw = value.get("split_with")
        split_with = LeaveType.parse(str(split_raw)).value if split_raw else None
    except (KeyError, TypeError, ValueError):
        return None
    if not plan_id or not dates or not fingerprint:
        return None
    reason = value.get("reason")
    return StoredPlan(
        plan_id, leave_type, dates, reason if isinstance(reason, str) and reason.strip() else None,
        split_with, fingerprint, expires_at,
    )
