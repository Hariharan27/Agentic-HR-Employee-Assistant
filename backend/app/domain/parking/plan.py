"""Parking plan: one or more dates booked with one chosen slot, confirmed together."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

PlanAction = Literal["reserve", "waitlist", "already_reserved", "already_waitlisted"]


@dataclass(frozen=True, slots=True)
class ParkingPlanLine:
    day: date
    action: PlanAction
    slot_code: str | None = None
    slot_id: int | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ParkingPlan:
    slot_code: str
    dates: tuple[date, ...]
    alternatives: tuple[tuple[date, str], ...]
    lines: tuple[ParkingPlanLine, ...]
    problems: tuple[str, ...]
    # Dates where the chosen slot is taken: the free slots there (or empty -> waitlist only).
    choices: tuple[tuple[date, tuple[str, ...]], ...] = ()
    vehicle: str = ""
    plan_id: str = ""
    expires_at: datetime | None = None
    fingerprint: str = field(default="")

    @property
    def actionable(self) -> tuple[ParkingPlanLine, ...]:
        return tuple(line for line in self.lines if line.action in {"reserve", "waitlist"})

    @property
    def eligible(self) -> bool:
        return not self.problems and bool(self.actionable)

    def compute_fingerprint(self) -> str:
        payload = [
            [line.day.isoformat(), line.action, line.slot_code] for line in self.lines
        ] + [list(self.problems), self.vehicle]
        return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]

    @staticmethod
    def _day(value: date) -> str:
        return f"{value.isoformat()} ({value.strftime('%a')})"

    def _line_text(self, line: ParkingPlanLine) -> str:
        if line.action == "reserve":
            return f"{self._day(line.day)}: slot {line.slot_code}"
        if line.action == "waitlist":
            return f"{self._day(line.day)}: waitlist (all slots taken)"
        if line.action == "already_reserved":
            return f"{self._day(line.day)}: you already have slot {line.slot_code}"
        return f"{self._day(line.day)}: you are already on the waitlist"

    def summary(self) -> str:
        text = "Parking plan:\n" + "\n".join(f"- {self._line_text(line)}" for line in self.lines)
        if self.problems:
            text += "\nNot ready: " + "; ".join(self.problems)
            text += "" if text.endswith(("?", ".")) else "."
        else:
            text += f"\nVehicle: {self.vehicle}."
        return text

    def confirmation_summary(self) -> str:
        parts = [self._line_text(line) for line in self.actionable]
        return f"Parking for {self.vehicle}: " + "; ".join(parts)

    def to_dict(self) -> dict[str, object]:
        return {
            "plan_id": self.plan_id,
            "slot_code": self.slot_code,
            "dates": [item.isoformat() for item in self.dates],
            "alternatives": {day.isoformat(): code for day, code in self.alternatives},
            "lines": [
                {"date": line.day.isoformat(), "action": line.action, "slot": line.slot_code, "note": line.note}
                for line in self.lines
            ],
            "choices": {day.isoformat(): list(codes) for day, codes in self.choices},
            "problems": list(self.problems),
            "eligible": self.eligible,
            "vehicle": self.vehicle,
            "summary": self.summary(),
            "fingerprint": self.fingerprint,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


def parking_plan_ttl() -> timedelta:
    return timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class StoredParkingPlan:
    plan_id: str
    slot_code: str
    dates: tuple[date, ...]
    alternatives: dict[date, str]
    fingerprint: str
    expires_at: datetime
    vehicle: str | None = None


def stored_parking_plan(value: object) -> StoredParkingPlan | None:
    if not isinstance(value, dict):
        return None
    try:
        dates = tuple(sorted({date.fromisoformat(str(item)) for item in value["dates"]}))
        alternatives = {
            date.fromisoformat(str(day)): str(code) for day, code in dict(value.get("alternatives") or {}).items()
        }
        stored = StoredParkingPlan(
            str(value["plan_id"]), str(value["slot_code"]), dates, alternatives,
            str(value["fingerprint"]), datetime.fromisoformat(str(value["expires_at"])),
            str(value.get("vehicle") or "") or None,
        )
    except (KeyError, TypeError, ValueError):
        return None
    return stored if stored.plan_id and stored.dates and stored.fingerprint else None
