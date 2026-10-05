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


@dataclass(frozen=True, slots=True)
class LeavePlan:
    leave_type: LeaveType
    requested_dates: tuple[date, ...]
    segments: tuple[PlanSegment, ...]
    excluded_days: tuple[ExcludedDay, ...]
    available_before: Decimal
    problems: tuple[str, ...]
    reason: str | None = None
    plan_id: str = ""
    expires_at: datetime | None = None
    fingerprint: str = field(default="")

    @property
    def total_working_days(self) -> Decimal:
        return sum((item.working_days for item in self.segments), Decimal("0"))

    @property
    def eligible(self) -> bool:
        return not self.problems and bool(self.segments)

    @property
    def available_after(self) -> Decimal:
        return self.available_before - self.total_working_days

    def compute_fingerprint(self) -> str:
        payload = {
            "type": self.leave_type.value,
            "segments": [
                [s.leave_type.value, s.start_date.isoformat(), s.end_date.isoformat(), _number(s.working_days)]
                for s in self.segments
            ],
            "excluded": [[e.day.isoformat(), e.reason] for e in self.excluded_days],
            "available": _number(self.available_before),
            "problems": list(self.problems),
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]

    def segment_text(self) -> str:
        parts = []
        for item in self.segments:
            if item.start_date == item.end_date:
                parts.append(f"{item.start_date.isoformat()} ({item.start_date.strftime('%a')})")
            else:
                parts.append(f"{item.start_date.isoformat()} to {item.end_date.isoformat()}")
        return "; ".join(parts)

    def excluded_text(self) -> str:
        return ", ".join(
            f"{item.day.isoformat()} ({item.day.strftime('%a')}, {item.reason})" for item in self.excluded_days
        )

    def summary(self) -> str:
        label = self.leave_type.value.title()
        if not self.segments:
            text = f"{label} leave: no working days in the requested dates."
        else:
            text = (
                f"{label} leave: {_number(self.total_working_days)} working day(s) on {self.segment_text()}."
            )
        if self.excluded_days:
            text += f" Not counted: {self.excluded_text()}."
        if self.eligible:
            text += (
                f" You are eligible. {label} balance {_number(self.available_before)} available, "
                f"{_number(self.available_after)} after this leave."
            )
        else:
            text += " You are not eligible: " + "; ".join(self.problems) + "."
        return text

    def confirmation_summary(self) -> str:
        label = self.leave_type.value.title()
        days = _number(self.total_working_days)
        if len(self.segments) == 1:
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
            "excluded_days": [{"date": item.day.isoformat(), "reason": item.reason} for item in self.excluded_days],
            "total_working_days": _number(self.total_working_days),
            "available_before": _number(self.available_before),
            "available_after": _number(self.available_after),
            "eligible": self.eligible,
            "problems": list(self.problems),
            "reason": self.reason,
            "summary": self.summary(),
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def plan_ttl() -> timedelta:
    return timedelta(minutes=30)


def stored_plan_inputs(value: object) -> tuple[str, tuple[date, ...], str | None, str, str, datetime] | None:
    """Validate a plan persisted on the conversation and return what is needed to rebuild it."""
    if not isinstance(value, dict):
        return None
    try:
        plan_id = str(value["plan_id"])
        leave_type = LeaveType.parse(str(value["leave_type"])).value
        dates = tuple(sorted({date.fromisoformat(str(item)) for item in value["requested_dates"]}))
        fingerprint = str(value["fingerprint"])
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if not plan_id or not dates or not fingerprint:
        return None
    reason = value.get("reason")
    return leave_type, dates, reason if isinstance(reason, str) and reason.strip() else None, plan_id, fingerprint, expires_at
