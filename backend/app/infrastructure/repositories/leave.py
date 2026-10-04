from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import ConflictError, NotFoundError
from app.domain.leave.entities import (
    LeaveBalanceSnapshot,
    LeaveRequestData,
    LeaveRequestEventData,
    LeaveStatus,
    LeaveType,
)
from app.infrastructure.database.models import Employee, Holiday, LeaveBalance, LeaveRequest, LeaveRequestEvent


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
        return [
            LeaveBalanceSnapshot(
                LeaveType.parse(row.leave_type),
                row.total_days,
                row.used_days,
                row.carry_forward_limit_days,
            )
            for row in rows
        ]

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
        manager_employee_id = self.db.scalar(
            select(Employee.manager_employee_id).where(Employee.id == request.employee_id)
        )
        row = LeaveRequest(
            employee_id=request.employee_id,
            manager_employee_id=manager_employee_id,
            leave_type=request.leave_type.value,
            start_date=request.start_date,
            end_date=request.end_date,
            working_days=request.working_days,
            reason=request.reason,
            status=request.status.value,
        )
        self.db.add(row)
        self.db.flush()
        return self._to_request_data(row)

    def list_requests(self, employee_id: int) -> list[LeaveRequestData]:
        statement = select(LeaveRequest).where(LeaveRequest.employee_id == employee_id).order_by(
            LeaveRequest.created_at.desc(), LeaveRequest.id.desc()
        )
        return [self._to_request_data(row) for row in self.db.scalars(statement).all()]

    def get_request(self, request_id: int, *, for_update: bool = False) -> LeaveRequestData | None:
        statement = select(LeaveRequest).where(LeaveRequest.id == request_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        return self._to_request_data(row) if row else None

    def list_managed_requests(
        self, manager_employee_id: int | None, status: LeaveStatus | None = None
    ) -> list[LeaveRequestData]:
        statement = select(LeaveRequest)
        if manager_employee_id is not None:
            statement = statement.where(LeaveRequest.manager_employee_id == manager_employee_id)
        if status is not None:
            statement = statement.where(LeaveRequest.status == status.value)
        statement = statement.order_by(LeaveRequest.created_at.asc(), LeaveRequest.id.asc())
        return [self._to_request_data(row) for row in self.db.scalars(statement).all()]

    def increase_used_days(self, employee_id: int, leave_type: LeaveType, days: Decimal) -> None:
        row = self.db.scalar(
            select(LeaveBalance).where(
                LeaveBalance.employee_id == employee_id,
                LeaveBalance.leave_type == leave_type.value,
            ).with_for_update()
        )
        if row is None:
            raise NotFoundError("No leave balance was found for the requested leave type")
        if row.used_days + days > row.total_days:
            raise ConflictError("The leave balance changed and can no longer cover this request")
        row.used_days += days
        self.db.flush()

    def update_request_status(
        self, request_id: int, status: LeaveStatus, actor_user_id: int, comment: str | None
    ) -> LeaveRequestData:
        row = self.db.get(LeaveRequest, request_id)
        if row is None:
            raise NotFoundError("Leave request was not found")
        row.status = status.value
        row.decided_by_user_id = actor_user_id
        row.decision_comment = comment
        row.decided_at = datetime.now(UTC)
        self.db.flush()
        return self._to_request_data(row)

    def add_event(self, event: LeaveRequestEventData) -> LeaveRequestEventData:
        row = LeaveRequestEvent(
            leave_request_id=event.leave_request_id,
            actor_user_id=event.actor_user_id,
            from_status=event.from_status.value if event.from_status else None,
            to_status=event.to_status.value,
            comment=event.comment,
        )
        self.db.add(row)
        self.db.flush()
        return LeaveRequestEventData(
            row.id, row.leave_request_id, row.actor_user_id,
            LeaveStatus(row.from_status) if row.from_status else None,
            LeaveStatus(row.to_status), row.comment, row.created_at,
        )

    def list_events(self, request_id: int) -> list[LeaveRequestEventData]:
        statement = select(LeaveRequestEvent).where(
            LeaveRequestEvent.leave_request_id == request_id
        ).order_by(LeaveRequestEvent.created_at.asc(), LeaveRequestEvent.id.asc())
        return [
            LeaveRequestEventData(
                row.id, row.leave_request_id, row.actor_user_id,
                LeaveStatus(row.from_status) if row.from_status else None,
                LeaveStatus(row.to_status), row.comment, row.created_at,
            )
            for row in self.db.scalars(statement).all()
        ]

    def _to_request_data(self, row: LeaveRequest) -> LeaveRequestData:
        employee = self.db.get(Employee, row.employee_id)
        return LeaveRequestData(
            row.id, row.employee_id, LeaveType.parse(row.leave_type), row.start_date, row.end_date,
            row.working_days, row.reason, LeaveStatus(row.status), row.manager_employee_id,
            row.decided_by_user_id, row.decision_comment, row.decided_at,
            employee.employee_code if employee else None,
            employee.name if employee else None,
        )

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
