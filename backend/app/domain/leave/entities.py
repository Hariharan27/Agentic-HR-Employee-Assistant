from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum


class LeaveType(StrEnum):
    CASUAL = "CASUAL"
    PRIVILEGE = "PRIVILEGE"
    SICK = "SICK"

    @classmethod
    def parse(cls, value: str) -> "LeaveType":
        normalized = value.strip().upper().replace(" ", "_")
        aliases = {"CL": cls.CASUAL, "PL": cls.PRIVILEGE, "SL": cls.SICK, "EARNED": cls.PRIVILEGE}
        return aliases.get(normalized, cls(normalized))


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

