from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class LeaveRequestResponse(BaseModel):
    id: int
    employee_id: int
    employee_code: str | None
    employee_name: str | None
    manager_employee_id: int | None
    leave_type: str
    start_date: date
    end_date: date
    working_days: Decimal
    reason: str | None
    status: str
    decided_by_user_id: int | None
    decision_comment: str | None
    decided_at: datetime | None


class ApprovalRequest(BaseModel):
    comment: str | None = Field(default=None, max_length=1000)


class RejectionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class CancellationRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class LeaveRequestEventResponse(BaseModel):
    id: int
    leave_request_id: int
    actor_user_id: int
    from_status: str | None
    to_status: str
    comment: str | None
    created_at: datetime | None
