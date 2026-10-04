from datetime import date

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.domain.parking.entities import (
    ParkingReservationData,
    ParkingReservationStatus,
    ParkingSlotData,
    ParkingSlotType,
    ParkingWaitlistData,
    ParkingWaitlistStatus,
    VehicleData,
    VehicleType,
)
from app.infrastructure.database.models import (
    ParkingReservation,
    ParkingSlot,
    ParkingWaitlistEntry,
    Vehicle,
)


class SQLAlchemyParkingRepository:
    """Read-side parking persistence used by the next workflow phase."""

    def __init__(self, db: Session):
        self.db = db

    def get_active_vehicle(self, employee_id: int) -> VehicleData | None:
        row = self.db.scalar(
            select(Vehicle).where(
                Vehicle.employee_id == employee_id,
                Vehicle.active.is_(True),
            )
        )
        return self._to_vehicle(row) if row else None

    def list_active_slots(self) -> list[ParkingSlotData]:
        rows = self.db.scalars(
            select(ParkingSlot)
            .where(ParkingSlot.active.is_(True))
            .order_by(ParkingSlot.code)
        ).all()
        return [self._to_slot(row) for row in rows]

    def list_available_slots(self, requested_date: date) -> list[ParkingSlotData]:
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
        rows = self.db.scalars(
            select(ParkingSlot)
            .where(ParkingSlot.active.is_(True), ~occupied)
            .order_by(ParkingSlot.code)
        ).all()
        return [self._to_slot(row) for row in rows]

    def get_active_reservation(
        self, employee_id: int, requested_date: date
    ) -> ParkingReservationData | None:
        row = self.db.scalar(
            select(ParkingReservation).where(
                ParkingReservation.employee_id == employee_id,
                ParkingReservation.reservation_date == requested_date,
                ParkingReservation.status.in_(
                    (
                        ParkingReservationStatus.RESERVED.value,
                        ParkingReservationStatus.CHECKED_IN.value,
                    )
                ),
            )
        )
        if row is None:
            return None
        slot = self.db.get(ParkingSlot, row.slot_id)
        if slot is None:
            raise NotFoundError("The parking slot for this reservation no longer exists")
        return self._to_reservation(row, slot)

    def get_waitlist_entry(
        self, employee_id: int, requested_date: date
    ) -> ParkingWaitlistData | None:
        row = self.db.scalar(
            select(ParkingWaitlistEntry).where(
                ParkingWaitlistEntry.employee_id == employee_id,
                ParkingWaitlistEntry.requested_date == requested_date,
                ParkingWaitlistEntry.status == ParkingWaitlistStatus.WAITING.value,
            )
        )
        return self._to_waitlist(row) if row else None

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
        )

    @classmethod
    def _to_reservation(
        cls, row: ParkingReservation, slot: ParkingSlot
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
