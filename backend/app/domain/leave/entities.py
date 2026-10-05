from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class LeaveType(StrEnum):
    CASUAL = "CASUAL"
    EARNED = "EARNED"
    SICK = "SICK"

    @classmethod
    def parse(cls, value: str) -> "LeaveType":
        normalized = value.strip().upper().replace(" ", "_")
        aliases = {
            "CL": cls.CASUAL,
            "SL": cls.SICK,
            "EL": cls.EARNED,
            "EARNED_LEAVE": cls.EARNED,
            "CASUAL_LEAVE": cls.CASUAL,
            "SICK_LEAVE": cls.SICK,
        }
        # Look up the alias first: dict.get(key, cls(key)) would evaluate cls(key) eagerly and fail.
        if normalized in aliases:
            return aliases[normalized]
        return cls(normalized)


class LeaveStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class LeaveBalanceSnapshot:
    leave_type: LeaveType
    total_days: Decimal
    used_days: Decimal
    carry_forward_limit_days: Decimal = Decimal("0")
    pending_days: Decimal = Decimal("0")

    @property
    def available_days(self) -> Decimal:
        return max(Decimal("0"), self.total_days - self.used_days - self.pending_days)


@dataclass(frozen=True, slots=True)
class LeaveEligibility:
    eligible: bool
    working_days: Decimal
    available_days: Decimal
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class LeaveRequestData:
    id: int | None
    employee_id: int
    leave_type: LeaveType
    start_date: date
    end_date: date
    working_days: Decimal
    reason: str | None
    status: LeaveStatus = LeaveStatus.PENDING
    manager_employee_id: int | None = None
    decided_by_user_id: int | None = None
    decision_comment: str | None = None
    decided_at: datetime | None = None
    employee_code: str | None = None
    employee_name: str | None = None


@dataclass(frozen=True, slots=True)
class LeaveRequestEventData:
    id: int | None
    leave_request_id: int
    actor_user_id: int
    from_status: LeaveStatus | None
    to_status: LeaveStatus
    comment: str | None
    created_at: datetime | None = None
