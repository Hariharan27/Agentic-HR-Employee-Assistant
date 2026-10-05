"""Typed employee parking tools for the Parking Agent, over ParkingService.

The employee chooses the slot: a slot code (or "waitlist") is accepted only if the user wrote it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from app.application.parking.service import ParkingService
from app.application.pending.service import PendingActionCoordinator
from app.core.exceptions import ApplicationError, AuthorizationError
from app.core.security import AuthenticatedUser
from app.domain.leave.dates import resolve_leave_dates
from app.domain.parking.plan import stored_parking_plan

logger = logging.getLogger("app.parking_tools")


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NoArguments(ToolArguments):
    pass


class ResolveDatesArguments(ToolArguments):
    text: str = Field(min_length=1, max_length=300)


class DatesArguments(ToolArguments):
    dates: list[date] = Field(min_length=1, max_length=14)


class BuildPlanArguments(ToolArguments):
    slot: str = Field(min_length=1, max_length=20, description="Slot code the user chose, e.g. B-22")
    dates: list[date] | None = Field(default=None, max_length=14, description="Defaults to the dates last listed")
    alternatives: dict[date, str] = Field(
        default_factory=dict,
        description="For dates where the chosen slot is taken: another slot code or 'waitlist', as the user chose",
    )
    vehicle: str | None = Field(
        default=None, max_length=32,
        description="Registration number of the vehicle the user chose; only needed when they have two",
    )


class PlanIdArguments(ToolArguments):
    plan_id: str = Field(min_length=1, max_length=40)


class CancelArguments(ToolArguments):
    date: date
    reason: str | None = Field(default=None, max_length=500)


@dataclass(frozen=True, slots=True)
class ParkingToolExecution:
    tool: str
    ok: bool
    data: dict[str, Any]
    label: str
    sources: list[dict[str, object]] = field(default_factory=list)
    pending_summary: str | None = None
    plan: dict[str, Any] | None = None
    pending_response: str | None = None


def _mentioned(code: str, text: str) -> bool:
    if code.strip().casefold() == "waitlist":
        return bool(re.search(r"\b(wait\s*list|waiting\s+list)\b", text, re.I))
    compact = re.sub(r"[\s-]", "", code).casefold()
    return compact in re.sub(r"[\s-]", "", text).casefold()


class ParkingToolExecutor:
    TOOLS: ClassVar[dict[str, tuple[type[ToolArguments], str, str]]] = {
        "resolve_dates": (
            ResolveDatesArguments,
            "Turn the user's date words, copied as written ('tomorrow', 'Mon to Wed', '12 Oct'), into exact dates.",
            "Resolved parking dates",
        ),
        "get_vehicle": (NoArguments, "The employee's registered vehicles (at most two).", "Checked registered vehicles"),
        "list_parking_slots": (
            DatesArguments,
            "Every parking slot with its status (free or taken) on each date, the employee's own bookings, "
            "and any date the employee cannot book.",
            "Checked parking slots",
        ),
        "build_parking_plan": (
            BuildPlanArguments,
            "Plan booking the slot the user chose for the dates; reports dates where it is taken with the "
            "free alternatives. Returns a plan_id when ready.",
            "Built parking plan",
        ),
        "prepare_parking": (
            PlanIdArguments, "Prepare a ready parking plan for explicit confirmation; never books it.",
            "Prepared parking reservation",
        ),
        "get_my_parking_reservations": (NoArguments, "The employee's parking reservations.", "Retrieved parking reservations"),
        "prepare_parking_cancellation": (
            CancelArguments, "Prepare cancelling the employee's reservation on a date, for confirmation.",
            "Prepared parking cancellation",
        ),
    }

    def __init__(
        self,
        *,
        actor: AuthenticatedUser,
        parking: ParkingService,
        pending: PendingActionCoordinator,
        today: Callable[[], date],
    ) -> None:
        self.actor = actor
        self.parking = parking
        self.pending = pending
        self.today = today
        self.turn_text = ""

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": spec[1], "arguments_schema": spec[0].model_json_schema()}
            for name, spec in self.TOOLS.items()
        ]

    def execute(self, tool: str, arguments: dict[str, Any], *, session_id: str,
                active_plan: dict[str, Any] | None = None) -> ParkingToolExecution:
        spec = self.TOOLS.get(tool)
        if spec is None:
            return self._error(tool, "Unsupported parking tool")
        if {"employee_id", "user_id", "actor_id"} & set(arguments):
            return self._error(tool, "Identity fields must come from the authenticated session")
        try:
            parsed = spec[0].model_validate(arguments)
        except PydanticValidationError as exc:
            fields = sorted({str(item["loc"][0]) for item in exc.errors() if item.get("loc")})
            return self._error(tool, "Missing or invalid tool arguments: " + ", ".join(fields))
        try:
            return getattr(self, f"_{tool}")(parsed, session_id=session_id, active=dict(active_plan or {}))
        except AuthorizationError:
            raise
        except ApplicationError as exc:
            return self._error(tool, str(exc))
        except Exception:
            logger.exception("parking_tool_failed", extra={"tool": tool, "session_id": session_id})
            return self._error(tool, "The parking service could not complete this operation")

    def _ok(self, tool: str, data: dict[str, Any], **extra: Any) -> ParkingToolExecution:
        return ParkingToolExecution(tool, True, data, self.TOOLS[tool][2], **extra)

    def _error(self, tool: str, message: str) -> ParkingToolExecution:
        label = self.TOOLS[tool][2] if tool in self.TOOLS else "Parking tool"
        return ParkingToolExecution(tool, False, {"error": message}, f"{label} failed")

    # ------------------------------------------------------------------ tools

    def _resolve_dates(self, arguments: ResolveDatesArguments, **_: Any):
        resolution = resolve_leave_dates(arguments.text, self.today())
        data: dict[str, Any] = {
            "shape": resolution.shape,
            "dates": [{"date": day.isoformat(), "weekday": day.strftime("%A")} for day in resolution.dates],
        }
        if resolution.ambiguous:
            data["question"] = "Please provide the parking date."
        return self._ok("resolve_dates", data)

    def _get_vehicle(self, arguments: NoArguments, **_: Any):
        vehicles = self.parking.list_vehicles(self.actor)
        return self._ok("get_vehicle", {"vehicles": [
            {
                "registration_number": vehicle.registration_number,
                "vehicle_type": vehicle.vehicle_type.value,
                "make_model": vehicle.make_model,
            }
            for vehicle in vehicles
        ]})

    def _list_parking_slots(self, arguments: DatesArguments, *, active: dict[str, Any], **_: Any):
        board = self.parking.slot_board(self.actor, arguments.dates)
        remembered = {"dates": [item.isoformat() for item in sorted(set(arguments.dates))]}
        return self._ok("list_parking_slots", board, plan=remembered)

    def _build_parking_plan(self, arguments: BuildPlanArguments, *, active: dict[str, Any], **_: Any):
        dates = arguments.dates or [date.fromisoformat(item) for item in active.get("dates", [])]
        if not dates:
            return self._error("build_parking_plan", "Please provide the parking date.")
        # The employee must have chosen every slot: in this message, or as the active plan's slot
        # (so "use B-24 on Thursday" can adjust a B-23 plan without repeating B-23).
        chosen_before = {str(active.get("slot_code") or "").upper()} - {""}
        unstated = [
            code
            for code in [arguments.slot, *arguments.alternatives.values()]
            if not _mentioned(code, self.turn_text) and str(code).upper() not in chosen_before
        ]
        if unstated:
            return self._error(
                "build_parking_plan",
                "The employee must choose the slot themselves; ask which slot they want (not chosen: "
                + ", ".join(unstated) + ")",
            )
        vehicle = arguments.vehicle or active.get("vehicle") or None
        if arguments.vehicle and not _mentioned(arguments.vehicle, self.turn_text) and (
            self.parking.normalize_registration(arguments.vehicle)
            != self.parking.normalize_registration(str(active.get("vehicle") or ""))
        ):
            return self._error(
                "build_parking_plan",
                "The employee must choose the vehicle themselves; ask which registered vehicle to use",
            )
        plan = self.parking.build_parking_plan(self.actor, dates, arguments.slot, arguments.alternatives, vehicle)
        data = plan.to_dict()
        return self._ok("build_parking_plan", data, plan=data)

    def _prepare_parking(self, arguments: PlanIdArguments, *, session_id: str, active: dict[str, Any], **_: Any):
        stored = stored_parking_plan(active)
        if stored is None or stored.plan_id != arguments.plan_id:
            return self._error("prepare_parking", "No parking plan has that plan_id; build the plan first")
        if stored.expires_at <= self.parking.now():
            return self._error("prepare_parking", "The parking plan expired; check the slots again")
        plan = self.parking.build_parking_plan(
            self.actor, stored.dates, stored.slot_code, stored.alternatives, stored.vehicle
        )
        if plan.compute_fingerprint() != stored.fingerprint:
            return self._error("prepare_parking", "Parking availability changed; check the slots again")
        if not plan.eligible:
            return self._error("prepare_parking", "; ".join(plan.problems) or "Nothing to book in this plan")
        summary = plan.confirmation_summary()
        action = self.pending.propose(
            self.actor, session_id, "reserve_parking_plan",
            {
                "dates": [item.isoformat() for item in stored.dates],
                "slot_code": stored.slot_code,
                "alternatives": {day.isoformat(): code for day, code in stored.alternatives.items()},
                "fingerprint": stored.fingerprint,
                "vehicle": stored.vehicle,
            },
            summary,
        )
        return self._ok("prepare_parking", {"prepared": True, "summary": action.summary},
                        pending_summary=action.summary, plan=active)

    def _get_my_parking_reservations(self, arguments: NoArguments, **_: Any):
        reservations = self.parking.get_my_reservations(self.actor)
        return self._ok("get_my_parking_reservations", {"reservations": [
            {"reservation_id": item.id, "date": item.reservation_date.isoformat(), "slot": item.slot.code,
             "status": item.status.value}
            for item in reservations[:20]
        ]})

    def _prepare_parking_cancellation(self, arguments: CancelArguments, *, session_id: str, **_: Any):
        reservation = self.parking.prepare_cancellation(self.actor, arguments.date)
        summary = (
            f"Cancel parking reservation #{reservation.id}, slot {reservation.slot.code}, "
            f"for {reservation.reservation_date}"
        )
        action = self.pending.propose(
            self.actor, session_id, "cancel_parking",
            {"reservation_id": reservation.id, "reason": arguments.reason}, summary,
        )
        return self._ok("prepare_parking_cancellation", {"prepared": True, "summary": action.summary},
                        pending_summary=action.summary)
