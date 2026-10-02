from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.domain.leave.entities import LeaveBalanceSnapshot, LeaveRequestData, LeaveStatus, LeaveType
from app.infrastructure.database.models import Holiday, LeaveBalance, LeaveRequest


class SQLAlchemyLeaveRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_balances(self, employee_id: int, leave_type: LeaveType | None = None,
                     *, for_update: bool = False) -> list[LeaveBalanceSnapshot]:
        statement = select(LeaveBalance).where(LeaveBalance.employee_id == employee_id)
        if leave_type is not None:
            statement = statement.where(LeaveBalance.leave_type == leave_type.value)
        if for_update:
            statement = statement.with_for_update()
        rows = self.db.scalars(statement.order_by(LeaveBalance.leave_type)).all()
        return [LeaveBalanceSnapshot(LeaveType(row.leave_type), row.total_days, row.used_days) for row in rows]

    def get_holidays(self, start_date: date, end_date: date) -> set[date]:
        statement = select(Holiday.holiday_date).where(Holiday.holiday_date.between(start_date, end_date))
        return set(self.db.scalars(statement).all())

    def get_pending_days(self, employee_id: int, leave_type: LeaveType) -> Decimal:
        statement = select(func.coalesce(func.sum(LeaveRequest.working_days), 0)).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.leave_type == leave_type.value,
            LeaveRequest.status == LeaveStatus.PENDING.value,
        )
        return Decimal(self.db.scalar(statement) or 0)

    def has_overlapping_request(self, employee_id: int, start_date: date, end_date: date) -> bool:
        statement = select(LeaveRequest.id).where(
            LeaveRequest.employee_id == employee_id,
            LeaveRequest.status.in_([LeaveStatus.PENDING.value, LeaveStatus.APPROVED.value]),
            LeaveRequest.start_date <= end_date,
            LeaveRequest.end_date >= start_date,
        ).limit(1)
        return self.db.scalar(statement) is not None

    def add_request(self, request: LeaveRequestData) -> LeaveRequestData:
        row = LeaveRequest(
            employee_id=request.employee_id,
            leave_type=request.leave_type.value,
            start_date=request.start_date,
            end_date=request.end_date,
            working_days=request.working_days,
            reason=request.reason,
            status=request.status.value,
        )
        self.db.add(row)
        self.db.flush()
        return LeaveRequestData(row.id, row.employee_id, LeaveType(row.leave_type), row.start_date, row.end_date,
                                row.working_days, row.reason, LeaveStatus(row.status))

    def list_requests(self, employee_id: int) -> list[LeaveRequestData]:
        statement = select(LeaveRequest).where(LeaveRequest.employee_id == employee_id).order_by(
            LeaveRequest.created_at.desc(), LeaveRequest.id.desc()
        )
        return [
            LeaveRequestData(row.id, row.employee_id, LeaveType(row.leave_type), row.start_date, row.end_date,
                             row.working_days, row.reason, LeaveStatus(row.status))
            for row in self.db.scalars(statement).all()
        ]

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
