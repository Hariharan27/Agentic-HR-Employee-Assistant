from collections.abc import Callable
from dataclasses import replace
from uuid import uuid4
from datetime import UTC, date, datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from app.application.parking.ports import ParkingRepository
from app.core.config import Settings
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ParkingUnavailableError,
    ValidationError,
)
from app.core.security import AuthenticatedUser, require_role
from app.domain.parking.plan import ParkingPlan, ParkingPlanLine, parking_plan_ttl
from app.domain.parking.entities import (
    ParkingAvailability,
    ParkingReservationData,
    ParkingReservationEventData,
    ParkingReservationStatus,
    ParkingSlotData,
    ParkingSuspensionData,
    ParkingWaitlistData,
    ParkingWaitlistStatus,
    VehicleData,
    VehicleType,
)


class ParkingService:
    """Deterministic employee parking use cases backed by transactional storage."""

    def __init__(
        self,
        repository: ParkingRepository,
        settings: Settings,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.settings = settings
        self.timezone = ZoneInfo(settings.app_timezone)
        self.now = now or (lambda: datetime.now(UTC))

    MAX_VEHICLES = 2

    @staticmethod
    def normalize_registration(value: str) -> str:
        return re.sub(r"[\s-]+", "", value or "").upper()

    def list_vehicles(self, actor: AuthenticatedUser) -> list[VehicleData]:
        return self.repository.list_active_vehicles(actor.employee_id)

    _TYPE_WORDS = (
        (re.compile(r"\b(bike|bikes|motorcycle|motorcycles|motorbike|scooter|scooty|two[- ]?wheeler|2[- ]?wheeler)\b", re.I), VehicleType.MOTORCYCLE),
        (re.compile(r"\b(car|cars|four[- ]?wheeler|4[- ]?wheeler)\b", re.I), VehicleType.CAR),
    )

    @classmethod
    def vehicle_type_in(cls, text: str | None) -> VehicleType | None:
        """The vehicle type a phrase names ("my car", "the bike"), if exactly one is named."""
        found = {kind for pattern, kind in cls._TYPE_WORDS if text and pattern.search(text)}
        return found.pop() if len(found) == 1 else None

    @staticmethod
    def type_label(kind: VehicleType) -> str:
        return "motorcycle" if kind is VehicleType.MOTORCYCLE else "car"

    @classmethod
    def describe_vehicles(cls, vehicles: list[VehicleData]) -> str:
        return ", ".join(f"{item.registration_number} ({cls.type_label(item.vehicle_type)})" for item in vehicles)

    def get_vehicle(self, actor: AuthenticatedUser, registration_number: str | None = None) -> VehicleData:
        """The employee's vehicle: the one named by registration (or part of it) or by type
        ("car", "bike"), or the only one. Two vehicles and no choice → ask."""
        vehicles = self.list_vehicles(actor)
        if not vehicles:
            raise NotFoundError(
                "No active vehicle is registered for your employee account. Register a vehicle first."
            )
        if registration_number:
            wanted = self.normalize_registration(registration_number)
            match = next((item for item in vehicles if item.registration_number == wanted), None)
            if match is not None:
                return match
            kind = self.vehicle_type_in(registration_number)
            if kind is not None:
                of_kind = [item for item in vehicles if item.vehicle_type is kind]
                if not of_kind:
                    raise NotFoundError(
                        f"You do not have a registered {self.type_label(kind)}. "
                        f"Your registered vehicles: {self.describe_vehicles(vehicles)}."
                    )
                if len(of_kind) == 1:
                    return of_kind[0]
                raise ValidationError(
                    f"You have two registered {self.type_label(kind)}s ({self.describe_vehicles(of_kind)}). "
                    "Which registration number should I use?"
                )
            partial = [item for item in vehicles if len(wanted) >= 3 and wanted in item.registration_number]
            if len(partial) == 1:
                return partial[0]
            raise NotFoundError(
                f"{wanted} is not one of your registered vehicles ({self.describe_vehicles(vehicles)})."
            )
        if len(vehicles) == 1:
            return vehicles[0]
        raise ValidationError(
            f"You have two registered vehicles ({' and '.join(f'{item.registration_number} ({self.type_label(item.vehicle_type)})' for item in vehicles)}). "
            "Which one should I use?"
        )

    @staticmethod
    def slots_for(slots, kind: VehicleType | None):
        """Only the slots a vehicle of this type may use (all slots when the type is unknown)."""
        return [slot for slot in slots if kind is None or slot.vehicle_type is kind]

    def upcoming_bookings(self, actor: AuthenticatedUser, vehicle: VehicleData) -> list[ParkingReservationData]:
        """Reserved or checked-in bookings from today on that use this vehicle."""
        today = self._local_now().date()
        return [
            item
            for item in self.repository.list_reservations(actor.employee_id)
            if item.vehicle_id == vehicle.id
            and item.reservation_date >= today
            and item.status in {ParkingReservationStatus.RESERVED, ParkingReservationStatus.CHECKED_IN}
        ]

    def _require_no_upcoming(self, actor: AuthenticatedUser, vehicle: VehicleData, action: str) -> None:
        upcoming = self.upcoming_bookings(actor, vehicle)
        if upcoming:
            bookings = ", ".join(f"#{item.id} on {item.reservation_date}" for item in upcoming[:5])
            raise ConflictError(
                f"Vehicle {vehicle.registration_number} has upcoming parking bookings ({bookings}). "
                f"Cancel them first, then {action} the vehicle."
            )

    def prepare_vehicle_registration(
        self,
        actor: AuthenticatedUser,
        registration_number: str,
        vehicle_type: str,
        make_model: str | None = None,
    ) -> tuple[str, VehicleType, str | None]:
        require_role(actor, "EMPLOYEE")
        normalized_registration = re.sub(r"[\s-]+", "", registration_number).upper()
        if not re.fullmatch(r"[A-Z0-9]{6,20}", normalized_registration):
            raise ValidationError(
                "Vehicle registration must contain 6 to 20 letters or numbers."
            )
        if not re.search(r"[A-Z]", normalized_registration) or not re.search(
            r"\d", normalized_registration
        ):
            raise ValidationError(
                "Vehicle registration must contain both letters and numbers."
            )
        normalized_type = vehicle_type.strip().upper().replace(" ", "_")
        normalized_type = {
            "BIKE": "MOTORCYCLE",
            "MOTORBIKE": "MOTORCYCLE",
            "TWO_WHEELER": "MOTORCYCLE",
        }.get(normalized_type, normalized_type)
        try:
            parsed_type = VehicleType(normalized_type)
        except ValueError as exc:
            raise ValidationError("Vehicle type must be Car or Motorcycle.") from exc
        normalized_model = make_model.strip() if make_model and make_model.strip() else None
        if normalized_model and len(normalized_model) > 120:
            raise ValidationError("Vehicle make and model must be 120 characters or fewer.")
        existing = self.repository.get_vehicle_by_registration(normalized_registration)
        if existing is not None and existing.employee_id != actor.employee_id:
            raise ConflictError("This vehicle registration is already assigned to another employee.")
        vehicles = self.list_vehicles(actor)
        current = next((item for item in vehicles if item.registration_number == normalized_registration), None)
        if current is not None:
            # Updating details is allowed only while no booking depends on the vehicle.
            self._require_no_upcoming(actor, current, "update")
        elif len(vehicles) >= self.MAX_VEHICLES:
            raise ConflictError(
                f"You already have {self.MAX_VEHICLES} registered vehicles ("
                + ", ".join(item.registration_number for item in vehicles)
                + "). Remove one before adding another."
            )
        return normalized_registration, parsed_type, normalized_model

    def is_registered(self, actor: AuthenticatedUser, registration_number: str) -> bool:
        wanted = self.normalize_registration(registration_number)
        return any(item.registration_number == wanted for item in self.list_vehicles(actor))

    def register_vehicle(
        self,
        actor: AuthenticatedUser,
        registration_number: str,
        vehicle_type: str,
        make_model: str | None = None,
    ) -> VehicleData:
        try:
            vehicle = self.stage_vehicle_registration(
                actor, registration_number, vehicle_type, make_model
            )
            self.repository.commit()
            return vehicle
        except Exception:
            self.repository.rollback()
            raise

    def stage_vehicle_registration(
        self,
        actor: AuthenticatedUser,
        registration_number: str,
        vehicle_type: str,
        make_model: str | None = None,
    ) -> VehicleData:
        normalized_registration, parsed_type, normalized_model = (
            self.prepare_vehicle_registration(
                actor, registration_number, vehicle_type, make_model
            )
        )
        return self.repository.upsert_vehicle(
            actor.employee_id,
            normalized_registration,
            parsed_type.value,
            normalized_model,
        )

    def prepare_vehicle_removal(
        self, actor: AuthenticatedUser, registration_number: str | None = None
    ) -> VehicleData:
        """The vehicle can be removed only when none of its bookings are still ahead."""
        require_role(actor, "EMPLOYEE")
        vehicle = self.get_vehicle(actor, registration_number)
        self._require_no_upcoming(actor, vehicle, "remove")
        return vehicle

    def stage_vehicle_removal(self, actor: AuthenticatedUser, registration_number: str) -> VehicleData:
        vehicle = self.prepare_vehicle_removal(actor, registration_number)
        removed = self.repository.deactivate_vehicle(vehicle.id)
        if removed is None:
            raise NotFoundError("That vehicle is no longer registered.")
        return removed

    def check_availability(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingAvailability:
        self._validate_booking_date(requested_date)
        vehicles = self.list_vehicles(actor)
        if not vehicles:
            raise NotFoundError("No active vehicle is registered for your employee account. Register a vehicle first.")
        vehicle = vehicles[0]
        slots = tuple(self.slots_for(self.repository.list_available_slots(requested_date), vehicle.vehicle_type))
        return ParkingAvailability(requested_date, vehicle, slots)

    def get_active_reservation(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingReservationData | None:
        return self.repository.get_active_reservation(actor.employee_id, requested_date)

    def get_my_reservations(self, actor: AuthenticatedUser) -> list[ParkingReservationData]:
        return self.repository.list_reservations(actor.employee_id)

    def get_waitlist_entry(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingWaitlistData | None:
        return self.repository.get_waitlist_entry(actor.employee_id, requested_date)

    def prepare_reservation(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> tuple[VehicleData, ParkingSlotData]:
        self._validate_booking_date(requested_date)
        self._validate_booking_eligibility(actor)
        existing = self.repository.get_active_reservation(actor.employee_id, requested_date)
        if existing is not None:
            raise ConflictError(
                f"You already have parking slot {existing.slot.code} reserved for {requested_date}."
            )
        vehicle = self.get_vehicle(actor)
        slots = self.slots_for(self.repository.list_available_slots(requested_date), vehicle.vehicle_type)
        if not slots:
            raise ParkingUnavailableError(
                f"No {self.type_label(vehicle.vehicle_type)} parking slots are available for {requested_date}."
            )
        return vehicle, slots[0]

    def reserve_parking(
        self, actor: AuthenticatedUser, requested_date: date, slot_id: int
    ) -> ParkingReservationData:
        try:
            reservation = self.stage_reservation(actor, requested_date, slot_id)
            self.repository.commit()
            return reservation
        except Exception:
            self.repository.rollback()
            raise

    def stage_reservation(
        self,
        actor: AuthenticatedUser,
        requested_date: date,
        slot_id: int,
        vehicle_registration: str | None = None,
    ) -> ParkingReservationData:
        self._validate_booking_date(requested_date)
        self._validate_booking_eligibility(actor)
        vehicle = self.get_vehicle(actor, vehicle_registration)
        existing = self.repository.get_active_reservation(
            actor.employee_id, requested_date, for_update=True
        )
        if existing is not None:
            raise ConflictError(
                f"You already have parking slot {existing.slot.code} reserved for {requested_date}."
            )
        available = self.repository.list_available_slots(requested_date, for_update=True)
        slot = next((item for item in available if item.id == slot_id), None)
        if slot is not None and slot.vehicle_type is not vehicle.vehicle_type:
            raise ValidationError(
                f"Slot {slot.code} is a {self.type_label(slot.vehicle_type)} slot, but "
                f"{vehicle.registration_number} is a {self.type_label(vehicle.vehicle_type)}."
            )
        if slot is None:
            raise ParkingUnavailableError(
                "The offered parking slot is no longer available. Please check availability again."
            )
        try:
            created = self.repository.add_reservation(
                actor.employee_id, vehicle.id, slot.id, requested_date
            )
        except IntegrityError as exc:
            raise ConflictError(
                "Parking availability changed while confirming. Please request a slot again."
            ) from exc
        self.repository.add_reservation_event(
            ParkingReservationEventData(
                id=0,
                reservation_id=created.id,
                actor_user_id=actor.user_id,
                from_status=None,
                to_status=ParkingReservationStatus.RESERVED,
                reason="Reserved by employee",
            )
        )
        waiting = self.repository.get_waitlist_entry(
            actor.employee_id, requested_date, for_update=True
        )
        if waiting is not None:
            self.repository.update_waitlist_status(waiting.id, ParkingWaitlistStatus.ALLOCATED)
        return created

    MAX_PLAN_DAYS = 14

    def slot_board(
        self,
        actor: AuthenticatedUser,
        dates: list[date] | tuple[date, ...],
        vehicle_registration: str | None = None,
    ) -> dict[str, object]:
        """The slots the employee's vehicle(s) may use, with their status on each date (who holds a
        slot is never shown). Car slots are only shown for a car, bike slots only for a motorcycle."""
        requested = sorted(set(dates))
        if not requested:
            raise ValidationError("Please provide the parking date.")
        if len(requested) > self.MAX_PLAN_DAYS:
            raise ValidationError(f"Parking can be planned for at most {self.MAX_PLAN_DAYS} days at once")
        vehicles = self.list_vehicles(actor)
        if vehicle_registration:
            vehicles = [self.get_vehicle(actor, vehicle_registration)]
        kinds = {item.vehicle_type for item in vehicles}
        slots = [slot for slot in self.repository.list_active_slots() if not kinds or slot.vehicle_type in kinds]
        days = []
        for day in requested:
            entry: dict[str, object] = {"date": day.isoformat(), "weekday": day.strftime("%A")}
            try:
                self._validate_booking_date(day)
            except ValidationError as exc:
                entry["unavailable_reason"] = str(exc)
            own = self.repository.get_active_reservation(actor.employee_id, day)
            if own is not None:
                entry["your_reservation"] = own.slot.code
            if self.repository.get_waitlist_entry(actor.employee_id, day) is not None:
                entry["you_are_waitlisted"] = True
            taken = self.repository.list_taken_slot_ids(day)
            entry["slots"] = [
                {
                    "slot": slot.code,
                    "type": slot.slot_type.value,
                    "vehicle_type": slot.vehicle_type.value,
                    "location": slot.location,
                    "status": "taken" if slot.id in taken else "free",
                }
                for slot in slots
            ]
            entry["free_slots"] = [slot.code for slot in slots if slot.id not in taken]
            days.append(entry)
        suspension = self.get_suspension(actor)
        return {
            "days": days,
            "vehicles": [
                {"registration_number": item.registration_number, "vehicle_type": item.vehicle_type.value}
                for item in vehicles
            ],
            "note": "Accessible slots are offered after the regular slots are taken, or when asked for. "
            "Car slots are for cars and motorcycle slots for motorcycles.",
            **({"suspended_until": suspension.suspended_until.isoformat()} if suspension.active else {}),
        }

    def build_parking_plan(
        self,
        actor: AuthenticatedUser,
        dates: list[date] | tuple[date, ...],
        slot_code: str,
        alternatives: dict[date, str] | None = None,
        vehicle_registration: str | None = None,
    ) -> ParkingPlan:
        """One chosen slot for every date; taken dates need an agreed alternative slot or the waitlist."""
        requested = tuple(sorted(set(dates)))
        if not requested:
            raise ValidationError("Please provide the parking date.")
        if len(requested) > self.MAX_PLAN_DAYS:
            raise ValidationError(f"Parking can be planned for at most {self.MAX_PLAN_DAYS} days at once")
        all_slots = {slot.code.upper(): slot for slot in self.repository.list_active_slots()}
        waitlist_only = slot_code.strip().upper() == "WAITLIST"
        chosen = None if waitlist_only else all_slots.get(slot_code.strip().upper())
        if chosen is None and not waitlist_only:
            raise ValidationError(f"There is no parking slot {slot_code}. Slots: {', '.join(sorted(all_slots))}")
        alternatives = {day: code.strip().upper() for day, code in (alternatives or {}).items()}
        if waitlist_only:
            alternatives = {day: "WAITLIST" for day in requested}
        problems: list[str] = []
        vehicle: VehicleData | None = None
        try:
            vehicle = self.get_vehicle(actor, vehicle_registration)
        except ValidationError as exc:  # two vehicles and none chosen yet
            # The chosen slot's type picks the vehicle when only one vehicle fits it.
            fitting = [
                item for item in self.list_vehicles(actor)
                if chosen is not None and item.vehicle_type is chosen.vehicle_type
            ]
            if len(fitting) == 1:
                vehicle = fitting[0]
            else:
                problems.append(str(exc))
        vehicle_text = vehicle.registration_number if vehicle else ""
        kind = vehicle.vehicle_type if vehicle else (chosen.vehicle_type if chosen else None)
        slots = {code: slot for code, slot in all_slots.items() if kind is None or slot.vehicle_type is kind}
        if vehicle is not None and chosen is not None and chosen.vehicle_type is not vehicle.vehicle_type:
            problems.append(
                f"Slot {chosen.code} is a {self.type_label(chosen.vehicle_type)} slot, but "
                f"{vehicle.registration_number} is a {self.type_label(vehicle.vehicle_type)}; "
                f"choose a {self.type_label(vehicle.vehicle_type)} slot ("
                + ", ".join(sorted(slots)) + ")"
            )
        lines: list[ParkingPlanLine] = []
        choices: list[tuple[date, tuple[str, ...]]] = []
        try:
            self._validate_booking_eligibility(actor)
        except ConflictError as exc:
            problems.append(str(exc))
        for day in requested:
            try:
                self._validate_booking_date(day)
            except ValidationError as exc:
                problems.append(f"{day.isoformat()}: {exc}")
                continue
            own = self.repository.get_active_reservation(actor.employee_id, day)
            if own is not None:
                lines.append(ParkingPlanLine(day, "already_reserved", own.slot.code, own.slot.id))
                continue
            if self.repository.get_waitlist_entry(actor.employee_id, day) is not None:
                lines.append(ParkingPlanLine(day, "already_waitlisted"))
                continue
            taken = self.repository.list_taken_slot_ids(day)
            free = tuple(code for code, slot in slots.items() if slot.id not in taken)
            wanted = alternatives.get(day, chosen.code.upper() if chosen else "WAITLIST")
            if wanted == "WAITLIST":
                if free:
                    problems.append(f"{day.isoformat()}: slots {', '.join(free)} are free, so the waitlist is not needed")
                else:
                    lines.append(ParkingPlanLine(day, "waitlist"))
                continue
            slot = slots.get(wanted)
            if slot is None and wanted in all_slots:
                if not (chosen is not None and wanted == chosen.code.upper() and vehicle is not None):
                    problems.append(
                        f"{day.isoformat()}: slot {wanted} is a "
                        f"{self.type_label(all_slots[wanted].vehicle_type)} slot"
                    )
            elif slot is None:
                problems.append(f"{day.isoformat()}: there is no parking slot {wanted}")
            elif slot.id in taken:
                choices.append((day, free))
                problems.append(
                    f"{day.isoformat()}: slot {slot.code} is taken; "
                    + (f"free slots: {', '.join(free)}" if free else "all slots are taken, so only the waitlist is possible")
                )
            else:
                lines.append(ParkingPlanLine(day, "reserve", slot.code, slot.id))
        plan = ParkingPlan(
            slot_code=chosen.code if chosen else "WAITLIST",
            dates=requested,
            alternatives=tuple(sorted(alternatives.items())),
            lines=tuple(lines),
            problems=tuple(problems),
            choices=tuple(choices),
            vehicle=vehicle_text,
        )
        return replace(
            plan,
            plan_id=f"pp_{uuid4().hex[:10]}",
            expires_at=self.now() + parking_plan_ttl(),
            fingerprint=plan.compute_fingerprint(),
        )

    def stage_parking_plan(
        self,
        actor: AuthenticatedUser,
        dates: list[date] | tuple[date, ...],
        slot_code: str,
        alternatives: dict[date, str] | None,
        expected_fingerprint: str,
        vehicle_registration: str | None = None,
    ) -> list[object]:
        """Rebuild the confirmed plan, require it unchanged, then reserve or waitlist each date."""
        plan = self.build_parking_plan(actor, dates, slot_code, alternatives, vehicle_registration)
        if plan.fingerprint != expected_fingerprint:
            raise ConflictError(
                "Parking availability changed since this confirmation was prepared. "
                "Cancel it and check the slots again"
            )
        if not plan.eligible:
            raise ValidationError("; ".join(plan.problems) or "The parking plan has nothing to book")
        results: list[object] = []
        for line in plan.actionable:
            if line.action == "reserve":
                results.append(self.stage_reservation(actor, line.day, line.slot_id, plan.vehicle or None))
            else:
                results.append(self.stage_join_waitlist(actor, line.day, plan.vehicle or None))
        return results

    def prepare_cancellation(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingReservationData:
        reservation = self.repository.get_active_reservation(actor.employee_id, requested_date)
        if reservation is None:
            raise NotFoundError(f"You do not have an active parking reservation for {requested_date}.")
        self._validate_employee_cancellation(reservation)
        return reservation

    def cancel_parking(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str | None = None
    ) -> ParkingReservationData:
        try:
            reservation = self.stage_cancellation(actor, reservation_id, reason)
            self.repository.commit()
            return reservation
        except Exception:
            self.repository.rollback()
            raise

    def stage_cancellation(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str | None = None
    ) -> ParkingReservationData:
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None:
            raise NotFoundError("Parking reservation was not found.")
        if reservation.employee_id != actor.employee_id:
            raise AuthorizationError("You can cancel only your own parking reservation.")
        self._validate_employee_cancellation(reservation)
        normalized_reason = self._normalize_reason(reason)
        updated = self.repository.update_reservation_status(
            reservation.id, ParkingReservationStatus.CANCELLED
        )
        self.repository.add_reservation_event(
            ParkingReservationEventData(
                id=0,
                reservation_id=reservation.id,
                actor_user_id=actor.user_id,
                from_status=ParkingReservationStatus.RESERVED,
                to_status=ParkingReservationStatus.CANCELLED,
                reason=normalized_reason or "Cancelled by employee before cutoff",
            )
        )
        return updated

    def prepare_waitlist(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> VehicleData:
        self._validate_booking_date(requested_date)
        self._validate_booking_eligibility(actor)
        vehicle = self.get_vehicle(actor)
        if self.repository.get_active_reservation(actor.employee_id, requested_date) is not None:
            raise ConflictError(f"You already have a parking reservation for {requested_date}.")
        if self.repository.get_waitlist_entry(actor.employee_id, requested_date) is not None:
            raise ConflictError(f"You are already on the parking waitlist for {requested_date}.")
        if self.slots_for(self.repository.list_available_slots(requested_date), vehicle.vehicle_type):
            raise ConflictError(
                f"A {self.type_label(vehicle.vehicle_type)} parking slot is currently available. "
                "Please reserve it instead of joining the waitlist."
            )
        return vehicle

    def join_waitlist(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingWaitlistData:
        try:
            entry = self.stage_join_waitlist(actor, requested_date)
            self.repository.commit()
            return entry
        except Exception:
            self.repository.rollback()
            raise

    def stage_join_waitlist(
        self, actor: AuthenticatedUser, requested_date: date, vehicle_registration: str | None = None
    ) -> ParkingWaitlistData:
        self._validate_booking_date(requested_date)
        self._validate_booking_eligibility(actor)
        vehicle = self.get_vehicle(actor, vehicle_registration)
        if self.repository.get_active_reservation(
            actor.employee_id, requested_date, for_update=True
        ) is not None:
            raise ConflictError(f"You already have a parking reservation for {requested_date}.")
        if self.repository.get_waitlist_entry(
            actor.employee_id, requested_date, for_update=True
        ) is not None:
            raise ConflictError(f"You are already on the parking waitlist for {requested_date}.")
        if self.slots_for(
            self.repository.list_available_slots(requested_date, for_update=True), vehicle.vehicle_type
        ):
            raise ConflictError(
                "A parking slot became available. Please reserve it instead of joining the waitlist."
            )
        try:
            return self.repository.add_waitlist_entry(
                actor.employee_id, vehicle.id, requested_date
            )
        except IntegrityError as exc:
            raise ConflictError(
                f"You are already on the parking waitlist for {requested_date}."
            ) from exc

    def get_suspension(self, actor: AuthenticatedUser) -> ParkingSuspensionData:
        today = self._local_now().date()
        since = today - timedelta(days=self.settings.parking_no_show_lookback_days - 1)
        no_shows = self.repository.list_no_show_reservations(actor.employee_id, since)
        if len(no_shows) < self.settings.parking_no_show_strike_limit:
            return ParkingSuspensionData(False, len(no_shows))
        latest = max(item.reservation_date for item in no_shows)
        suspended_until = latest + timedelta(days=self.settings.parking_suspension_days)
        return ParkingSuspensionData(today <= suspended_until, len(no_shows), suspended_until)

    def get_daily_reservations(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> list[ParkingReservationData]:
        require_role(actor, "PARKING_ADMIN")
        return self.repository.list_reservations_for_date(requested_date)

    def prepare_check_in(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str | None = None
    ) -> ParkingReservationData:
        require_role(actor, "PARKING_ADMIN")
        reservation = self._get_reservation(reservation_id)
        if reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("Only a reserved parking booking can be checked in.")
        current = self._local_now()
        if reservation.reservation_date != current.date():
            raise ConflictError("A reservation can be checked in only on its reservation date.")
        opens_at = datetime.combine(
            reservation.reservation_date,
            time(self.settings.parking_check_in_open_hour),
            tzinfo=self.timezone,
        )
        if current < opens_at:
            raise ConflictError(
                f"Check-in opens at {opens_at.strftime('%H:%M')} on the reservation date."
            )
        if current > self._no_show_deadline(reservation.reservation_date):
            self._require_reason(reason, "A reason is required for a late check-in.")
        return reservation

    def stage_check_in(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str | None = None
    ) -> ParkingReservationData:
        self.prepare_check_in(actor, reservation_id, reason)
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None or reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("The reservation is no longer available for check-in.")
        normalized_reason = self._normalize_reason(reason)
        updated = self.repository.update_reservation_status(
            reservation_id, ParkingReservationStatus.CHECKED_IN
        )
        self._add_admin_event(
            actor,
            reservation,
            ParkingReservationStatus.CHECKED_IN,
            normalized_reason or "Arrival verified by Parking Administrator",
        )
        return updated

    def prepare_admin_cancellation(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str
    ) -> ParkingReservationData:
        require_role(actor, "PARKING_ADMIN")
        self._require_reason(reason, "A reason is required for an administrator cancellation.")
        reservation = self._get_reservation(reservation_id)
        if reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("Only a reserved parking booking can be cancelled.")
        return reservation

    def stage_admin_cancellation(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str
    ) -> ParkingReservationData:
        self.prepare_admin_cancellation(actor, reservation_id, reason)
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None or reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("The reservation is no longer available for cancellation.")
        normalized_reason = self._require_reason(
            reason, "A reason is required for an administrator cancellation."
        )
        updated = self.repository.update_reservation_status(
            reservation_id, ParkingReservationStatus.CANCELLED
        )
        self._add_admin_event(
            actor, reservation, ParkingReservationStatus.CANCELLED, normalized_reason
        )
        return updated

    def prepare_no_show(
        self, actor: AuthenticatedUser, reservation_id: int
    ) -> ParkingReservationData:
        require_role(actor, "PARKING_ADMIN")
        reservation = self._get_reservation(reservation_id)
        if reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("Only a reserved parking booking can be marked as a no-show.")
        if self._local_now() < self._no_show_deadline(reservation.reservation_date):
            deadline = self._no_show_deadline(reservation.reservation_date)
            raise ConflictError(
                f"This reservation cannot be marked as a no-show before {deadline.strftime('%Y-%m-%d %H:%M')}."
            )
        return reservation

    def stage_no_show(
        self, actor: AuthenticatedUser, reservation_id: int
    ) -> ParkingReservationData:
        self.prepare_no_show(actor, reservation_id)
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None or reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError("The reservation is no longer available for no-show processing.")
        updated = self.repository.update_reservation_status(
            reservation_id, ParkingReservationStatus.NO_SHOW
        )
        self._add_admin_event(
            actor,
            reservation,
            ParkingReservationStatus.NO_SHOW,
            "Arrival cutoff and grace period elapsed",
        )
        return updated

    def prepare_no_show_override(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str
    ) -> ParkingReservationData:
        require_role(actor, "PARKING_ADMIN")
        self._require_reason(reason, "A reason is required to correct a no-show.")
        reservation = self._get_reservation(reservation_id)
        if reservation.status is not ParkingReservationStatus.NO_SHOW:
            raise ConflictError("Only a no-show reservation can be corrected.")
        return reservation

    def stage_no_show_override(
        self, actor: AuthenticatedUser, reservation_id: int, reason: str
    ) -> ParkingReservationData:
        self.prepare_no_show_override(actor, reservation_id, reason)
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None or reservation.status is not ParkingReservationStatus.NO_SHOW:
            raise ConflictError("The reservation is no longer recorded as a no-show.")
        normalized_reason = self._require_reason(
            reason, "A reason is required to correct a no-show."
        )
        updated = self.repository.update_reservation_status(
            reservation_id, ParkingReservationStatus.CANCELLED
        )
        self._add_admin_event(
            actor, reservation, ParkingReservationStatus.CANCELLED, normalized_reason
        )
        return updated

    def prepare_completion(
        self, actor: AuthenticatedUser, reservation_id: int
    ) -> ParkingReservationData:
        require_role(actor, "PARKING_ADMIN")
        reservation = self._get_reservation(reservation_id)
        if reservation.status is not ParkingReservationStatus.CHECKED_IN:
            raise ConflictError("Only a checked-in reservation can be completed.")
        return reservation

    def stage_completion(
        self, actor: AuthenticatedUser, reservation_id: int
    ) -> ParkingReservationData:
        self.prepare_completion(actor, reservation_id)
        reservation = self.repository.get_reservation(reservation_id, for_update=True)
        if reservation is None or reservation.status is not ParkingReservationStatus.CHECKED_IN:
            raise ConflictError("The reservation is no longer available for completion.")
        updated = self.repository.update_reservation_status(
            reservation_id, ParkingReservationStatus.COMPLETED
        )
        self._add_admin_event(
            actor,
            reservation,
            ParkingReservationStatus.COMPLETED,
            "Parking visit completed by administrator",
        )
        return updated

    def get_reservation_history(
        self, actor: AuthenticatedUser, reservation_id: int
    ) -> list[ParkingReservationEventData]:
        reservation = self._get_reservation(reservation_id)
        if reservation.employee_id != actor.employee_id:
            require_role(actor, "PARKING_ADMIN")
        return self.repository.list_reservation_events(reservation_id)

    def _validate_booking_date(self, requested_date: date) -> None:
        current = self._local_now()
        today = current.date()
        if requested_date < today:
            raise ValidationError("Parking cannot be reserved for a past date.")
        if requested_date > today + timedelta(days=self.settings.parking_booking_horizon_days):
            raise ValidationError(
                f"Parking can be reserved only {self.settings.parking_booking_horizon_days} days in advance."
            )
        if requested_date == today and current.time() >= time(
            self.settings.parking_arrival_cutoff_hour
        ):
            raise ValidationError("Today's parking reservation cutoff has passed.")

    def _validate_booking_eligibility(self, actor: AuthenticatedUser) -> None:
        suspension = self.get_suspension(actor)
        if suspension.active:
            raise ConflictError(
                f"Parking reservations are suspended through {suspension.suspended_until} because "
                f"you have {suspension.no_show_count} no-shows in the last "
                f"{self.settings.parking_no_show_lookback_days} days. Contact the Parking Administrator "
                "if a no-show is incorrect."
            )

    def _validate_employee_cancellation(self, reservation: ParkingReservationData) -> None:
        if reservation.status is not ParkingReservationStatus.RESERVED:
            raise ConflictError(
                "Only a reserved parking booking can be cancelled by the employee."
            )
        cutoff_date = reservation.reservation_date - timedelta(days=1)
        cutoff = datetime.combine(
            cutoff_date,
            time(self.settings.parking_cancellation_cutoff_hour),
            tzinfo=self.timezone,
        )
        if self._local_now() >= cutoff:
            raise ConflictError(
                "The employee cancellation cutoff has passed. Contact the Parking Administrator for help."
            )

    def _local_now(self) -> datetime:
        current = self.now()
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current.astimezone(self.timezone)

    def _no_show_deadline(self, reservation_date: date) -> datetime:
        cutoff = datetime.combine(
            reservation_date,
            time(self.settings.parking_arrival_cutoff_hour),
            tzinfo=self.timezone,
        )
        return cutoff + timedelta(minutes=self.settings.parking_no_show_grace_minutes)

    def _get_reservation(self, reservation_id: int) -> ParkingReservationData:
        reservation = self.repository.get_reservation(reservation_id)
        if reservation is None:
            raise NotFoundError("Parking reservation was not found.")
        return reservation

    def _add_admin_event(
        self,
        actor: AuthenticatedUser,
        reservation: ParkingReservationData,
        to_status: ParkingReservationStatus,
        reason: str,
    ) -> None:
        self.repository.add_reservation_event(
            ParkingReservationEventData(
                id=0,
                reservation_id=reservation.id,
                actor_user_id=actor.user_id,
                from_status=reservation.status,
                to_status=to_status,
                reason=reason,
            )
        )

    @staticmethod
    def _normalize_reason(reason: str | None) -> str | None:
        normalized = reason.strip() if reason else ""
        if len(normalized) > 1000:
            raise ValidationError("Cancellation reason cannot exceed 1000 characters.")
        return normalized or None

    @classmethod
    def _require_reason(cls, reason: str | None, message: str) -> str:
        normalized = cls._normalize_reason(reason)
        if normalized is None:
            raise ValidationError(message)
        return normalized
