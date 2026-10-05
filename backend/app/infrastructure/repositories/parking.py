from datetime import UTC, date, datetime

from sqlalchemy import case, exists, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.domain.parking.entities import (
    ParkingReservationData,
    ParkingReservationEventData,
    ParkingReservationStatus,
    ParkingSlotData,
    ParkingSlotType,
    ParkingWaitlistData,
    ParkingWaitlistStatus,
    VehicleData,
    VehicleType,
)
from app.infrastructure.database.models import (
    Employee,
    ParkingReservation,
    ParkingReservationEvent,
    ParkingSlot,
    ParkingWaitlistEntry,
    Vehicle,
)


class SQLAlchemyParkingRepository:
    """Read-side parking persistence used by the next workflow phase."""

    def __init__(self, db: Session):
        self.db = db

    def list_active_vehicles(self, employee_id: int) -> list[VehicleData]:
        rows = self.db.scalars(
            select(Vehicle)
            .where(Vehicle.employee_id == employee_id, Vehicle.active.is_(True))
            .order_by(Vehicle.id)
        ).all()
        return [self._to_vehicle(row) for row in rows]

    def get_active_vehicle(self, employee_id: int) -> VehicleData | None:
        vehicles = self.list_active_vehicles(employee_id)
        return vehicles[0] if vehicles else None

    def deactivate_vehicle(self, vehicle_id: int) -> VehicleData | None:
        """Soft-remove: past reservations keep pointing at the vehicle row for the audit trail."""
        row = self.db.get(Vehicle, vehicle_id)
        if row is None or not row.active:
            return None
        row.active = False
        self.db.flush()
        return self._to_vehicle(row)

    def get_vehicle_by_registration(self, registration_number: str) -> VehicleData | None:
        row = self.db.scalar(
            select(Vehicle).where(Vehicle.registration_number == registration_number)
        )
        return self._to_vehicle(row) if row else None

    def upsert_vehicle(
        self,
        employee_id: int,
        registration_number: str,
        vehicle_type: str,
        make_model: str | None,
    ) -> VehicleData:
        """Add a vehicle, or update/reactivate the employee's vehicle with this registration."""
        row = self.db.scalar(
            select(Vehicle).where(
                Vehicle.employee_id == employee_id,
                Vehicle.registration_number == registration_number,
            )
        )
        if row is None:
            row = Vehicle(
                employee_id=employee_id,
                registration_number=registration_number,
                vehicle_type=vehicle_type,
                make_model=make_model,
                active=True,
            )
            self.db.add(row)
        else:
            row.vehicle_type = vehicle_type
            row.make_model = make_model
            row.active = True
        self.db.flush()
        return self._to_vehicle(row)

    @staticmethod
    def _slot_order():
        # Regular slots first; accessible slots are offered last.
        return (
            ParkingSlot.vehicle_type,
            case((ParkingSlot.slot_type == ParkingSlotType.ACCESSIBLE.value, 1), else_=0),
            ParkingSlot.code,
        )

    def list_active_slots(self) -> list[ParkingSlotData]:
        rows = self.db.scalars(
            select(ParkingSlot)
            .where(ParkingSlot.active.is_(True))
            .order_by(*self._slot_order())
        ).all()
        return [self._to_slot(row) for row in rows]

    def list_taken_slot_ids(self, requested_date: date) -> set[int]:
        statement = select(ParkingReservation.slot_id).where(
            ParkingReservation.reservation_date == requested_date,
            ParkingReservation.status.in_(
                (ParkingReservationStatus.RESERVED.value, ParkingReservationStatus.CHECKED_IN.value)
            ),
        )
        return set(self.db.scalars(statement).all())

    def list_available_slots(
        self, requested_date: date, *, for_update: bool = False
    ) -> list[ParkingSlotData]:
        occupied = exists().where(
            ParkingReservation.slot_id == ParkingSlot.id,
            ParkingReservation.reservation_date == requested_date,
            ParkingReservation.status.in_(
                (
                    ParkingReservationStatus.RESERVED.value,
                    ParkingReservationStatus.CHECKED_IN.value,
                )
            ),
        )
        statement = (
            select(ParkingSlot)
            .where(
                ParkingSlot.active.is_(True),
                ~occupied,
            )
            .order_by(*self._slot_order())
        )
        if for_update:
            statement = statement.with_for_update(skip_locked=True)
        rows = self.db.scalars(statement).all()
        return [self._to_slot(row) for row in rows]

    def get_active_reservation(
        self, employee_id: int, requested_date: date, *, for_update: bool = False
    ) -> ParkingReservationData | None:
        statement = select(ParkingReservation).where(
                ParkingReservation.employee_id == employee_id,
                ParkingReservation.reservation_date == requested_date,
                ParkingReservation.status.in_(
                    (
                        ParkingReservationStatus.RESERVED.value,
                        ParkingReservationStatus.CHECKED_IN.value,
                    )
                )
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        if row is None:
            return None
        return self._reservation_data(row)

    def get_reservation(
        self, reservation_id: int, *, for_update: bool = False
    ) -> ParkingReservationData | None:
        statement = select(ParkingReservation).where(ParkingReservation.id == reservation_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        return self._reservation_data(row) if row else None

    def list_reservations(self, employee_id: int) -> list[ParkingReservationData]:
        rows = self.db.scalars(
            select(ParkingReservation)
            .where(ParkingReservation.employee_id == employee_id)
            .order_by(
                ParkingReservation.reservation_date.desc(),
                ParkingReservation.id.desc(),
            )
        ).all()
        return [self._reservation_data(row) for row in rows]

    def list_reservations_for_date(
        self, requested_date: date
    ) -> list[ParkingReservationData]:
        rows = self.db.scalars(
            select(ParkingReservation)
            .where(ParkingReservation.reservation_date == requested_date)
            .order_by(ParkingReservation.slot_id, ParkingReservation.id)
        ).all()
        return [self._reservation_data(row) for row in rows]

    def list_no_show_reservations(
        self, employee_id: int, since: date
    ) -> list[ParkingReservationData]:
        rows = self.db.scalars(
            select(ParkingReservation)
            .where(
                ParkingReservation.employee_id == employee_id,
                ParkingReservation.status == ParkingReservationStatus.NO_SHOW.value,
                ParkingReservation.reservation_date >= since,
            )
            .order_by(ParkingReservation.reservation_date.desc())
        ).all()
        return [self._reservation_data(row) for row in rows]

    def add_reservation(
        self, employee_id: int, vehicle_id: int, slot_id: int, requested_date: date
    ) -> ParkingReservationData:
        row = ParkingReservation(
            employee_id=employee_id,
            vehicle_id=vehicle_id,
            slot_id=slot_id,
            reservation_date=requested_date,
            status=ParkingReservationStatus.RESERVED.value,
        )
        self.db.add(row)
        self.db.flush()
        return self._reservation_data(row)

    def update_reservation_status(
        self, reservation_id: int, status: ParkingReservationStatus
    ) -> ParkingReservationData:
        row = self.db.get(ParkingReservation, reservation_id)
        if row is None:
            raise NotFoundError("Parking reservation was not found")
        row.status = status.value
        now = datetime.now(UTC)
        if status is ParkingReservationStatus.CANCELLED:
            row.cancelled_at = now
        elif status is ParkingReservationStatus.CHECKED_IN:
            row.checked_in_at = now
        elif status is ParkingReservationStatus.COMPLETED:
            row.completed_at = now
        elif status is ParkingReservationStatus.NO_SHOW:
            row.no_show_at = now
        self.db.flush()
        return self._reservation_data(row)

    def add_reservation_event(
        self, event: ParkingReservationEventData
    ) -> ParkingReservationEventData:
        row = ParkingReservationEvent(
            reservation_id=event.reservation_id,
            actor_user_id=event.actor_user_id,
            from_status=event.from_status.value if event.from_status else None,
            to_status=event.to_status.value,
            reason=event.reason,
        )
        self.db.add(row)
        self.db.flush()
        return ParkingReservationEventData(
            row.id,
            row.reservation_id,
            row.actor_user_id,
            ParkingReservationStatus(row.from_status) if row.from_status else None,
            ParkingReservationStatus(row.to_status),
            row.reason,
            row.created_at,
        )

    def list_reservation_events(
        self, reservation_id: int
    ) -> list[ParkingReservationEventData]:
        rows = self.db.scalars(
            select(ParkingReservationEvent)
            .where(ParkingReservationEvent.reservation_id == reservation_id)
            .order_by(ParkingReservationEvent.created_at, ParkingReservationEvent.id)
        ).all()
        return [
            ParkingReservationEventData(
                row.id,
                row.reservation_id,
                row.actor_user_id,
                ParkingReservationStatus(row.from_status) if row.from_status else None,
                ParkingReservationStatus(row.to_status),
                row.reason,
                row.created_at,
            )
            for row in rows
        ]

    def get_waitlist_entry(
        self, employee_id: int, requested_date: date, *, for_update: bool = False
    ) -> ParkingWaitlistData | None:
        statement = select(ParkingWaitlistEntry).where(
                ParkingWaitlistEntry.employee_id == employee_id,
                ParkingWaitlistEntry.requested_date == requested_date,
                ParkingWaitlistEntry.status == ParkingWaitlistStatus.WAITING.value,
        )
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        return self._to_waitlist(row) if row else None

    def add_waitlist_entry(
        self, employee_id: int, vehicle_id: int, requested_date: date
    ) -> ParkingWaitlistData:
        row = ParkingWaitlistEntry(
            employee_id=employee_id,
            vehicle_id=vehicle_id,
            requested_date=requested_date,
            status=ParkingWaitlistStatus.WAITING.value,
        )
        self.db.add(row)
        self.db.flush()
        return self._to_waitlist(row)

    def update_waitlist_status(
        self, entry_id: int, status: ParkingWaitlistStatus
    ) -> ParkingWaitlistData:
        row = self.db.get(ParkingWaitlistEntry, entry_id)
        if row is None:
            raise NotFoundError("Parking waitlist entry was not found")
        row.status = status.value
        self.db.flush()
        return self._to_waitlist(row)

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()

    def _reservation_data(self, row: ParkingReservation) -> ParkingReservationData:
        slot = self.db.get(ParkingSlot, row.slot_id)
        if slot is None:
            raise NotFoundError("The parking slot for this reservation no longer exists")
        employee = self.db.get(Employee, row.employee_id)
        vehicle = self.db.get(Vehicle, row.vehicle_id)
        return self._to_reservation(row, slot, employee, vehicle)

    @staticmethod
    def _to_vehicle(row: Vehicle) -> VehicleData:
        return VehicleData(
            row.id,
            row.employee_id,
            row.registration_number,
            VehicleType(row.vehicle_type),
            row.make_model,
            row.active,
        )

    @staticmethod
    def _to_slot(row: ParkingSlot) -> ParkingSlotData:
        return ParkingSlotData(
            row.id,
            row.code,
            row.location,
            ParkingSlotType(row.slot_type),
            row.active,
            VehicleType(row.vehicle_type or "CAR"),
        )

    @classmethod
    def _to_reservation(
        cls,
        row: ParkingReservation,
        slot: ParkingSlot,
        employee: Employee | None = None,
        vehicle: Vehicle | None = None,
    ) -> ParkingReservationData:
        return ParkingReservationData(
            row.id,
            row.employee_id,
            row.vehicle_id,
            cls._to_slot(slot),
            row.reservation_date,
            ParkingReservationStatus(row.status),
            row.cancelled_at,
            row.checked_in_at,
            row.completed_at,
            row.no_show_at,
            row.created_at,
            row.updated_at,
            employee.employee_code if employee else None,
            employee.name if employee else None,
            vehicle.registration_number if vehicle else None,
        )

    @staticmethod
    def _to_waitlist(row: ParkingWaitlistEntry) -> ParkingWaitlistData:
        return ParkingWaitlistData(
            row.id,
            row.employee_id,
            row.vehicle_id,
            row.requested_date,
            ParkingWaitlistStatus(row.status),
            row.created_at,
            row.updated_at,
        )
