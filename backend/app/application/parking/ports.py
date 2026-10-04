from datetime import date
from typing import Protocol

from app.domain.parking.entities import (
    ParkingReservationData,
    ParkingSlotData,
    ParkingWaitlistData,
    VehicleData,
)


class ParkingRepository(Protocol):
    def get_active_vehicle(self, employee_id: int) -> VehicleData | None: ...
    def list_active_slots(self) -> list[ParkingSlotData]: ...
    def list_available_slots(self, requested_date: date) -> list[ParkingSlotData]: ...
    def get_active_reservation(
        self, employee_id: int, requested_date: date
    ) -> ParkingReservationData | None: ...
    def get_waitlist_entry(
        self, employee_id: int, requested_date: date
    ) -> ParkingWaitlistData | None: ...
