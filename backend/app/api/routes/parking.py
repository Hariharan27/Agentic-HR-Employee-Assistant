from datetime import date

from fastapi import APIRouter, Query

from app.api.dependencies import AppSettings, CurrentUser, Database
from app.api.schemas.parking import (
    ParkingReservationEventResponse,
    ParkingReservationResponse,
    ParkingSuspensionResponse,
)
from app.application.parking.service import ParkingService
from app.domain.parking.entities import ParkingReservationData
from app.infrastructure.repositories.parking import SQLAlchemyParkingRepository


router = APIRouter(prefix="/api/v1/parking", tags=["employee parking"])
admin_router = APIRouter(
    prefix="/api/v1/parking-admin", tags=["parking administration"]
)


def _service(db: Database, settings: AppSettings) -> ParkingService:
    return ParkingService(SQLAlchemyParkingRepository(db), settings)


def _reservation_response(item: ParkingReservationData) -> ParkingReservationResponse:
    return ParkingReservationResponse(
        id=item.id,
        employee_id=item.employee_id,
        employee_code=item.employee_code,
        employee_name=item.employee_name,
        vehicle_registration=item.vehicle_registration,
        slot_code=item.slot.code,
        slot_location=item.slot.location,
        reservation_date=item.reservation_date,
        status=item.status.value,
        cancelled_at=item.cancelled_at,
        checked_in_at=item.checked_in_at,
        completed_at=item.completed_at,
        no_show_at=item.no_show_at,
    )


@router.get("/me/suspension", response_model=ParkingSuspensionResponse)
def my_parking_suspension(
    actor: CurrentUser, db: Database, settings: AppSettings
) -> ParkingSuspensionResponse:
    status = _service(db, settings).get_suspension(actor)
    return ParkingSuspensionResponse(
        active=status.active,
        no_show_count=status.no_show_count,
        suspended_until=status.suspended_until,
    )


@router.get(
    "/reservations/{reservation_id}/history",
    response_model=list[ParkingReservationEventResponse],
)
def parking_reservation_history(
    reservation_id: int,
    actor: CurrentUser,
    db: Database,
    settings: AppSettings,
) -> list[ParkingReservationEventResponse]:
    events = _service(db, settings).get_reservation_history(actor, reservation_id)
    return [
        ParkingReservationEventResponse(
            id=item.id,
            reservation_id=item.reservation_id,
            actor_user_id=item.actor_user_id,
            from_status=item.from_status.value if item.from_status else None,
            to_status=item.to_status.value,
            reason=item.reason,
            created_at=item.created_at,
        )
        for item in events
    ]


@admin_router.get("/reservations", response_model=list[ParkingReservationResponse])
def daily_parking_reservations(
    actor: CurrentUser,
    db: Database,
    settings: AppSettings,
    reservation_date: date = Query(),
) -> list[ParkingReservationResponse]:
    return [
        _reservation_response(item)
        for item in _service(db, settings).get_daily_reservations(
            actor, reservation_date
        )
    ]
