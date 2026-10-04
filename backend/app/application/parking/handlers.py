from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.application.parking.service import ParkingService
from app.core.exceptions import ValidationError
from app.core.security import AuthenticatedUser


class ReserveParkingArguments(BaseModel):
    requested_date: date
    slot_id: int = Field(gt=0)


class CancelParkingArguments(BaseModel):
    reservation_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class JoinParkingWaitlistArguments(BaseModel):
    requested_date: date


class ReserveParkingHandler:
    action_type = "reserve_parking"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return ReserveParkingArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for parking reservation") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ReserveParkingArguments.model_validate(arguments)
        return self.parking_service.stage_reservation(
            actor, validated.requested_date, validated.slot_id
        )


class CancelParkingHandler:
    action_type = "cancel_parking"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return CancelParkingArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for parking cancellation") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = CancelParkingArguments.model_validate(arguments)
        return self.parking_service.stage_cancellation(
            actor, validated.reservation_id, validated.reason
        )


class JoinParkingWaitlistHandler:
    action_type = "join_parking_waitlist"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return JoinParkingWaitlistArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for the parking waitlist") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = JoinParkingWaitlistArguments.model_validate(arguments)
        return self.parking_service.stage_join_waitlist(actor, validated.requested_date)
