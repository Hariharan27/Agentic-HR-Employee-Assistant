from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
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
from app.core.security import AuthenticatedUser
from app.domain.parking.entities import (
    ParkingAvailability,
    ParkingReservationData,
    ParkingReservationEventData,
    ParkingReservationStatus,
    ParkingSlotData,
    ParkingWaitlistData,
    ParkingWaitlistStatus,
    VehicleData,
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

    def get_vehicle(self, actor: AuthenticatedUser) -> VehicleData:
        vehicle = self.repository.get_active_vehicle(actor.employee_id)
        if vehicle is None:
            raise NotFoundError(
                "No active vehicle is registered for your employee account. Contact Workplace Operations."
            )
        return vehicle

    def check_availability(
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingAvailability:
        self._validate_booking_date(requested_date)
        vehicle = self.get_vehicle(actor)
        slots = tuple(self.repository.list_available_slots(requested_date))
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
        existing = self.repository.get_active_reservation(actor.employee_id, requested_date)
        if existing is not None:
            raise ConflictError(
                f"You already have parking slot {existing.slot.code} reserved for {requested_date}."
            )
        vehicle = self.get_vehicle(actor)
        slots = self.repository.list_available_slots(requested_date)
        if not slots:
            raise ParkingUnavailableError(f"No regular parking slots are available for {requested_date}.")
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
        self, actor: AuthenticatedUser, requested_date: date, slot_id: int
    ) -> ParkingReservationData:
        self._validate_booking_date(requested_date)
        vehicle = self.get_vehicle(actor)
        existing = self.repository.get_active_reservation(
            actor.employee_id, requested_date, for_update=True
        )
        if existing is not None:
            raise ConflictError(
                f"You already have parking slot {existing.slot.code} reserved for {requested_date}."
            )
        available = self.repository.list_available_slots(requested_date, for_update=True)
        slot = next((item for item in available if item.id == slot_id), None)
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
        vehicle = self.get_vehicle(actor)
        if self.repository.get_active_reservation(actor.employee_id, requested_date) is not None:
            raise ConflictError(f"You already have a parking reservation for {requested_date}.")
        if self.repository.get_waitlist_entry(actor.employee_id, requested_date) is not None:
            raise ConflictError(f"You are already on the parking waitlist for {requested_date}.")
        if self.repository.list_available_slots(requested_date):
            raise ConflictError(
                "A regular parking slot is currently available. Please reserve it instead of joining the waitlist."
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
        self, actor: AuthenticatedUser, requested_date: date
    ) -> ParkingWaitlistData:
        self._validate_booking_date(requested_date)
        vehicle = self.get_vehicle(actor)
        if self.repository.get_active_reservation(
            actor.employee_id, requested_date, for_update=True
        ) is not None:
            raise ConflictError(f"You already have a parking reservation for {requested_date}.")
        if self.repository.get_waitlist_entry(
            actor.employee_id, requested_date, for_update=True
        ) is not None:
            raise ConflictError(f"You are already on the parking waitlist for {requested_date}.")
        if self.repository.list_available_slots(requested_date, for_update=True):
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

    @staticmethod
    def _normalize_reason(reason: str | None) -> str | None:
        normalized = reason.strip() if reason else ""
        if len(normalized) > 1000:
            raise ValidationError("Cancellation reason cannot exceed 1000 characters.")
        return normalized or None
