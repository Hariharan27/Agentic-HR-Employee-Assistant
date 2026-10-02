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
