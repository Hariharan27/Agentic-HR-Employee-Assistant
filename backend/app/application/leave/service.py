from datetime import date
from decimal import Decimal

from app.application.leave.ports import LeaveRepository
from app.core.exceptions import ConflictError, InsufficientLeaveError, NotFoundError, ValidationError
from app.core.security import AuthenticatedUser
from app.domain.leave.entities import LeaveBalanceSnapshot, LeaveEligibility, LeaveRequestData, LeaveType
from app.domain.leave.rules import calculate_working_days, validate_date_range


class LeaveService:
    """Deterministic leave use cases. Employee identity always comes from trusted auth context."""

    def __init__(self, repository: LeaveRepository):
        self.repository = repository

    @staticmethod
    def _parse_leave_type(value: str) -> LeaveType:
        try:
            return LeaveType.parse(value)
        except ValueError as exc:
            supported = ", ".join(item.value for item in LeaveType)
            raise ValidationError(f"Unsupported leave type. Supported values: {supported}") from exc

    def get_leave_balance(self, actor: AuthenticatedUser, leave_type: str | None = None) -> list[LeaveBalanceSnapshot]:
        parsed = self._parse_leave_type(leave_type) if leave_type else None
        balances = self.repository.get_balances(actor.employee_id, parsed)
        if not balances:
            raise NotFoundError("No leave balance was found for the authenticated employee")
        return [
            LeaveBalanceSnapshot(b.leave_type, b.total_days, b.used_days,
                                 self.repository.get_pending_days(actor.employee_id, b.leave_type))
            for b in balances
        ]

    def get_holidays(self, start_date: date, end_date: date) -> set[date]:
        validate_date_range(start_date, end_date)
        return self.repository.get_holidays(start_date, end_date)

    def calculate_leave_days(self, start_date: date, end_date: date) -> Decimal:
        return calculate_working_days(start_date, end_date, self.get_holidays(start_date, end_date))

    def check_leave_eligibility(self, actor: AuthenticatedUser, leave_type: str,
                                start_date: date, end_date: date) -> LeaveEligibility:
        parsed = self._parse_leave_type(leave_type)
        working_days = self.calculate_leave_days(start_date, end_date)
        if working_days == 0:
            return LeaveEligibility(False, working_days, Decimal("0"), "The selected range has no working days")
        balances = self.get_leave_balance(actor, parsed.value)
        available = balances[0].available_days
        if self.repository.has_overlapping_request(actor.employee_id, start_date, end_date):
            return LeaveEligibility(False, working_days, available, "An active leave request overlaps this date range")
        if working_days > available:
            return LeaveEligibility(False, working_days, available, "Insufficient available leave balance")
        return LeaveEligibility(True, working_days, available)

    def apply_leave(self, actor: AuthenticatedUser, leave_type: str, start_date: date,
                    end_date: date, reason: str | None = None) -> LeaveRequestData:
        try:
            created = self.stage_leave_application(actor, leave_type, start_date, end_date, reason)
            self.repository.commit()
            return created
        except Exception:
            self.repository.rollback()
            raise

    def stage_leave_application(self, actor: AuthenticatedUser, leave_type: str, start_date: date,
                                end_date: date, reason: str | None = None) -> LeaveRequestData:
        """Revalidate and stage a request without committing, for an outer confirmation transaction."""
        parsed = self._parse_leave_type(leave_type)
        locked = self.repository.get_balances(actor.employee_id, parsed, for_update=True)
        if not locked:
            raise NotFoundError("No leave balance was found for the requested leave type")
        working_days = calculate_working_days(start_date, end_date,
                                              self.repository.get_holidays(start_date, end_date))
        if working_days == 0:
            raise ValidationError("The selected range has no working days")
        if self.repository.has_overlapping_request(actor.employee_id, start_date, end_date):
            raise ConflictError("An active leave request overlaps this date range")
        pending = self.repository.get_pending_days(actor.employee_id, parsed)
        available = locked[0].total_days - locked[0].used_days - pending
        if working_days > available:
            raise InsufficientLeaveError(
                f"The request needs {working_days} day(s), but only {max(Decimal('0'), available)} are available"
            )
        request = LeaveRequestData(None, actor.employee_id, parsed, start_date, end_date,
                                   working_days, reason.strip() if reason and reason.strip() else None)
        return self.repository.add_request(request)

    def get_my_leave_requests(self, actor: AuthenticatedUser) -> list[LeaveRequestData]:
        return self.repository.list_requests(actor.employee_id)
