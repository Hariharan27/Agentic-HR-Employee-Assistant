from datetime import date
from typing import Any

from pydantic import BaseModel, Field, ValidationError as PydanticValidationError

from app.application.leave.service import LeaveService
from app.core.security import AuthenticatedUser
from app.core.exceptions import ValidationError


class ApplyLeaveArguments(BaseModel):
    leave_type: str = Field(min_length=1, max_length=32)
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=1000)


class ApplyLeaveHandler:
    action_type = "apply_leave"

    def __init__(self, leave_service: LeaveService):
        self.leave_service = leave_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return ApplyLeaveArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for leave application") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = ApplyLeaveArguments.model_validate(arguments)
        return self.leave_service.stage_leave_application(
            actor,
            validated.leave_type,
            validated.start_date,
            validated.end_date,
            validated.reason,
        )


class LeaveRequestDecisionArguments(BaseModel):
    request_id: int = Field(gt=0)
    comment: str | None = Field(default=None, max_length=1000)


class RejectLeaveRequestArguments(BaseModel):
    request_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


class CancelLeaveRequestArguments(BaseModel):
    request_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class ApproveLeaveRequestHandler:
    action_type = "approve_leave_request"

    def __init__(self, leave_service: LeaveService):
        self.leave_service = leave_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return LeaveRequestDecisionArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for leave approval") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = LeaveRequestDecisionArguments.model_validate(arguments)
        return self.leave_service.stage_approve_leave_request(
            actor, validated.request_id, validated.comment
        )


class RejectLeaveRequestHandler:
    action_type = "reject_leave_request"

    def __init__(self, leave_service: LeaveService):
        self.leave_service = leave_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return RejectLeaveRequestArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for leave rejection") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = RejectLeaveRequestArguments.model_validate(arguments)
        return self.leave_service.stage_reject_leave_request(
            actor, validated.request_id, validated.reason
        )


class CancelLeaveRequestHandler:
    action_type = "cancel_leave_request"

    def __init__(self, leave_service: LeaveService):
        self.leave_service = leave_service

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            return CancelLeaveRequestArguments.model_validate(arguments).model_dump(mode="json")
        except PydanticValidationError as exc:
            raise ValidationError("Invalid arguments for leave cancellation") from exc

    def execute(self, actor: AuthenticatedUser, arguments: dict[str, Any]):
        validated = CancelLeaveRequestArguments.model_validate(arguments)
        return self.leave_service.stage_cancel_leave_request(
            actor, validated.request_id, validated.reason
        )
