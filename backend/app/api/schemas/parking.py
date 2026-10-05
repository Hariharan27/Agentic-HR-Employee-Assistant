from datetime import date, datetime

from pydantic import BaseModel


class ParkingReservationResponse(BaseModel):
    id: int
    employee_id: int
    employee_code: str | None
    employee_name: str | None
    vehicle_registration: str | None
    slot_code: str
    slot_location: str
    reservation_date: date
    status: str
    cancelled_at: datetime | None
    checked_in_at: datetime | None
    completed_at: datetime | None
    no_show_at: datetime | None


class ParkingReservationEventResponse(BaseModel):
    id: int
    reservation_id: int
    actor_user_id: int | None
    from_status: str | None
    to_status: str
    reason: str | None
    created_at: datetime | None


class ParkingSuspensionResponse(BaseModel):
    active: bool
    no_show_count: int
    suspended_until: date | None


class VehicleResponse(BaseModel):
    registration_number: str
    vehicle_type: str
    make_model: str | None = None
