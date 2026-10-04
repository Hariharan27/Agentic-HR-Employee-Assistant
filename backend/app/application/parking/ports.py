from datetime import date
from typing import Protocol

from app.domain.parking.entities import (
    ParkingReservationData,
    ParkingReservationEventData,
    ParkingReservationStatus,
    ParkingSlotData,
    ParkingWaitlistData,
    ParkingWaitlistStatus,
    VehicleData,
)


class ParkingRepository(Protocol):
    def get_active_vehicle(self, employee_id: int) -> VehicleData | None: ...
    def list_active_slots(self) -> list[ParkingSlotData]: ...
    def list_available_slots(
        self, requested_date: date, *, for_update: bool = False
    ) -> list[ParkingSlotData]: ...
    def get_active_reservation(
        self, employee_id: int, requested_date: date, *, for_update: bool = False
    ) -> ParkingReservationData | None: ...
    def get_reservation(
        self, reservation_id: int, *, for_update: bool = False
    ) -> ParkingReservationData | None: ...
    def list_reservations(self, employee_id: int) -> list[ParkingReservationData]: ...
    def add_reservation(
        self, employee_id: int, vehicle_id: int, slot_id: int, requested_date: date
    ) -> ParkingReservationData: ...
    def update_reservation_status(
        self, reservation_id: int, status: ParkingReservationStatus
    ) -> ParkingReservationData: ...
    def add_reservation_event(
        self, event: ParkingReservationEventData
    ) -> ParkingReservationEventData: ...
    def get_waitlist_entry(
        self, employee_id: int, requested_date: date, *, for_update: bool = False
    ) -> ParkingWaitlistData | None: ...
    def add_waitlist_entry(
        self, employee_id: int, vehicle_id: int, requested_date: date
    ) -> ParkingWaitlistData: ...
    def update_waitlist_status(
        self, entry_id: int, status: ParkingWaitlistStatus
    ) -> ParkingWaitlistData: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
