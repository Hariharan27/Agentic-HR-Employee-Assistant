from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum


class VehicleType(StrEnum):
    CAR = "CAR"
    MOTORCYCLE = "MOTORCYCLE"


class ParkingSlotType(StrEnum):
    REGULAR = "REGULAR"
    ACCESSIBLE = "ACCESSIBLE"


class ParkingReservationStatus(StrEnum):
    RESERVED = "RESERVED"
    CHECKED_IN = "CHECKED_IN"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"
    NO_SHOW = "NO_SHOW"


class ParkingWaitlistStatus(StrEnum):
    WAITING = "WAITING"
    ALLOCATED = "ALLOCATED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class ParkingAvailability:
    requested_date: date
    vehicle: "VehicleData"
    available_slots: tuple["ParkingSlotData", ...]


@dataclass(frozen=True, slots=True)
class VehicleData:
    id: int
    employee_id: int
    registration_number: str
    vehicle_type: VehicleType
    make_model: str | None
    active: bool


@dataclass(frozen=True, slots=True)
class ParkingSlotData:
    id: int
    code: str
    location: str
    slot_type: ParkingSlotType
    active: bool


@dataclass(frozen=True, slots=True)
class ParkingReservationData:
    id: int
    employee_id: int
    vehicle_id: int
    slot: ParkingSlotData
    reservation_date: date
    status: ParkingReservationStatus
    cancelled_at: datetime | None = None
    checked_in_at: datetime | None = None
    completed_at: datetime | None = None
    no_show_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParkingReservationEventData:
    id: int
    reservation_id: int
    actor_user_id: int | None
    from_status: ParkingReservationStatus | None
    to_status: ParkingReservationStatus
    reason: str | None
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ParkingWaitlistData:
    id: int
    employee_id: int
    vehicle_id: int
    requested_date: date
    status: ParkingWaitlistStatus
    created_at: datetime | None = None
    updated_at: datetime | None = None
