from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.application.parking.service import ParkingService
from app.core.exceptions import ValidationError
from app.core.security import AuthenticatedUser


class RegisterVehicleArguments(BaseModel):
    registration_number: str = Field(min_length=1, max_length=32)
    vehicle_type: str = Field(min_length=1, max_length=24)
    make_model: str | None = Field(default=None, max_length=120)


class ReserveParkingArguments(BaseModel):
    requested_date: date
    slot_id: int = Field(gt=0)


class CancelParkingArguments(BaseModel):
    reservation_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class JoinParkingWaitlistArguments(BaseModel):
    requested_date: date


class ParkingAdminActionArguments(BaseModel):
    reservation_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class RegisterVehicleHandler:
    action_type = "register_vehicle"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return RegisterVehicleArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for vehicle registration") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = RegisterVehicleArguments.model_validate(arguments)
        return self.parking_service.stage_vehicle_registration(
            actor,
            validated.registration_number,
            validated.vehicle_type,
            validated.make_model,
        )


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


class ReserveParkingPlanArguments(BaseModel):
    dates: list[date] = Field(min_length=1, max_length=14)
    slot_code: str = Field(min_length=1, max_length=20)
    alternatives: dict[date, str] = Field(default_factory=dict)
    fingerprint: str = Field(min_length=8, max_length=64)


class ReserveParkingPlanHandler:
    """Confirm a parking plan: rebuild it, require it unchanged, reserve or waitlist each date."""

    action_type = "reserve_parking_plan"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return ReserveParkingPlanArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for parking reservation") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ReserveParkingPlanArguments.model_validate(arguments)
        return self.parking_service.stage_parking_plan(
            actor, validated.dates, validated.slot_code, validated.alternatives, validated.fingerprint
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


class CheckInParkingHandler:
    action_type = "check_in_parking"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _admin_arguments(arguments, "parking check-in")

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ParkingAdminActionArguments.model_validate(arguments)
        return self.parking_service.stage_check_in(
            actor, validated.reservation_id, validated.reason
        )


class AdminCancelParkingHandler:
    action_type = "admin_cancel_parking"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _admin_arguments(arguments, "administrator parking cancellation")

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ParkingAdminActionArguments.model_validate(arguments)
        return self.parking_service.stage_admin_cancellation(
            actor, validated.reservation_id, validated.reason or ""
        )


class MarkParkingNoShowHandler:
    action_type = "mark_parking_no_show"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _admin_arguments(arguments, "parking no-show")

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ParkingAdminActionArguments.model_validate(arguments)
        return self.parking_service.stage_no_show(actor, validated.reservation_id)


class OverrideParkingNoShowHandler:
    action_type = "override_parking_no_show"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _admin_arguments(arguments, "parking no-show correction")

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ParkingAdminActionArguments.model_validate(arguments)
        return self.parking_service.stage_no_show_override(
            actor, validated.reservation_id, validated.reason or ""
        )


class CompleteParkingHandler:
    action_type = "complete_parking"

    def __init__(self, parking_service: ParkingService):
        self.parking_service = parking_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return _admin_arguments(arguments, "parking completion")

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ParkingAdminActionArguments.model_validate(arguments)
        return self.parking_service.stage_completion(actor, validated.reservation_id)


def _admin_arguments(arguments: dict[str, Any], action: str) -> dict[str, Any]:
    try:
        return ParkingAdminActionArguments.model_validate(arguments).model_dump(mode="json")
    except PydanticValidationError as exc:
        raise ValidationError(f"Invalid arguments for {action}") from exc
