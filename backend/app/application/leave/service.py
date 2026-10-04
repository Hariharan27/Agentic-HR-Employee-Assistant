from datetime import date
from decimal import Decimal

from app.application.leave.ports import LeaveRepository
from app.core.exceptions import (
    AuthorizationError,
    ConflictError,
    InsufficientLeaveError,
    NotFoundError,
    ValidationError,
)
from app.core.security import AuthenticatedUser, require_role
from app.domain.leave.entities import (
    LeaveBalanceSnapshot,
    LeaveEligibility,
    LeaveRequestData,
    LeaveRequestEventData,
    LeaveStatus,
    LeaveType,
)
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
            LeaveBalanceSnapshot(
                b.leave_type,
                b.total_days,
                b.used_days,
                b.carry_forward_limit_days,
                pending_days=self.repository.get_pending_days(actor.employee_id, b.leave_type),
            )
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
        created = self.repository.add_request(request)
        self.repository.add_event(
            LeaveRequestEventData(
                None, created.id, actor.user_id, None, LeaveStatus.PENDING, "Submitted by employee"
            )
        )
        return created

    def get_my_leave_requests(self, actor: AuthenticatedUser) -> list[LeaveRequestData]:
        return self.repository.list_requests(actor.employee_id)

    def get_managed_leave_requests(
        self, actor: AuthenticatedUser, status: str | None = "PENDING"
    ) -> list[LeaveRequestData]:
        require_role(actor, "MANAGER", "HR")
        parsed_status = self._parse_status(status) if status else None
        manager_scope = None if actor.role.upper() == "HR" else actor.employee_id
        return self.repository.list_managed_requests(manager_scope, parsed_status)

    def prepare_leave_decision(
        self, actor: AuthenticatedUser, request_id: int
    ) -> LeaveRequestData:
        """Authorize and preview a pending request without changing it."""
        require_role(actor, "MANAGER", "HR")
        request = self.repository.get_request(request_id)
        if request is None:
            raise NotFoundError("Leave request was not found")
        self._authorize_approver(actor, request)
        self._require_pending(request)
        return request

    def prepare_leave_cancellation(
        self, actor: AuthenticatedUser, request_id: int
    ) -> LeaveRequestData:
        """Authorize and preview cancellation of an employee's own pending request."""
        request = self.repository.get_request(request_id)
        if request is None:
            raise NotFoundError("Leave request was not found")
        if request.employee_id != actor.employee_id:
            raise AuthorizationError("You can cancel only your own leave requests")
        self._require_pending(request)
        return request

    def approve_leave_request(
        self, actor: AuthenticatedUser, request_id: int, comment: str | None = None
    ) -> LeaveRequestData:
        try:
            updated = self.stage_approve_leave_request(actor, request_id, comment)
            self.repository.commit()
            return updated
        except Exception:
            self.repository.rollback()
            raise

    def stage_approve_leave_request(
        self, actor: AuthenticatedUser, request_id: int, comment: str | None = None
    ) -> LeaveRequestData:
        """Stage an approval for an outer pending-action transaction."""
        request = self._pending_request_for_decision(actor, request_id)
        locked_balance = self.repository.get_balances(
            request.employee_id, request.leave_type, for_update=True
        )
        if not locked_balance:
            raise NotFoundError("No leave balance was found for the requested leave type")
        pending = self.repository.get_pending_days(request.employee_id, request.leave_type)
        other_pending = max(Decimal("0"), pending - request.working_days)
        remaining = locked_balance[0].total_days - locked_balance[0].used_days - other_pending
        if request.working_days > remaining:
            raise ConflictError("The leave balance changed and can no longer cover this request")
        normalized_comment = self._comment(comment)
        self.repository.increase_used_days(
            request.employee_id, request.leave_type, request.working_days
        )
        updated = self.repository.update_request_status(
            request_id, LeaveStatus.APPROVED, actor.user_id, normalized_comment
        )
        self.repository.add_event(
            LeaveRequestEventData(
                None, request_id, actor.user_id, LeaveStatus.PENDING,
                LeaveStatus.APPROVED, normalized_comment,
            )
        )
        return updated

    def reject_leave_request(
        self, actor: AuthenticatedUser, request_id: int, reason: str
    ) -> LeaveRequestData:
        try:
            updated = self.stage_reject_leave_request(actor, request_id, reason)
            self.repository.commit()
            return updated
        except Exception:
            self.repository.rollback()
            raise

    def stage_reject_leave_request(
        self, actor: AuthenticatedUser, request_id: int, reason: str
    ) -> LeaveRequestData:
        """Stage a rejection for an outer pending-action transaction."""
        comment = self._comment(reason, required=True)
        self._pending_request_for_decision(actor, request_id)
        updated = self.repository.update_request_status(
            request_id, LeaveStatus.REJECTED, actor.user_id, comment
        )
        self.repository.add_event(
            LeaveRequestEventData(
                None, request_id, actor.user_id, LeaveStatus.PENDING,
                LeaveStatus.REJECTED, comment,
            )
        )
        return updated

    def cancel_leave_request(
        self, actor: AuthenticatedUser, request_id: int, reason: str | None = None
    ) -> LeaveRequestData:
        try:
            updated = self.stage_cancel_leave_request(actor, request_id, reason)
            self.repository.commit()
            return updated
        except Exception:
            self.repository.rollback()
            raise

    def stage_cancel_leave_request(
        self, actor: AuthenticatedUser, request_id: int, reason: str | None = None
    ) -> LeaveRequestData:
        """Stage an employee cancellation for an outer pending-action transaction."""
        request = self.repository.get_request(request_id, for_update=True)
        if request is None:
            raise NotFoundError("Leave request was not found")
        if request.employee_id != actor.employee_id:
            raise AuthorizationError("You can cancel only your own leave requests")
        self._require_pending(request)
        comment = self._comment(reason)
        updated = self.repository.update_request_status(
            request_id, LeaveStatus.CANCELLED, actor.user_id, comment
        )
        self.repository.add_event(
            LeaveRequestEventData(
                None, request_id, actor.user_id, LeaveStatus.PENDING,
                LeaveStatus.CANCELLED, comment,
            )
        )
        return updated

    def get_leave_request_history(
        self, actor: AuthenticatedUser, request_id: int
    ) -> list[LeaveRequestEventData]:
        request = self.repository.get_request(request_id)
        if request is None:
            raise NotFoundError("Leave request was not found")
        if request.employee_id != actor.employee_id:
            self._authorize_approver(actor, request)
        return self.repository.list_events(request_id)

    def _pending_request_for_decision(
        self, actor: AuthenticatedUser, request_id: int
    ) -> LeaveRequestData:
        require_role(actor, "MANAGER", "HR")
        request = self.repository.get_request(request_id, for_update=True)
        if request is None:
            raise NotFoundError("Leave request was not found")
        self._authorize_approver(actor, request)
        self._require_pending(request)
        return request

    @staticmethod
    def _authorize_approver(actor: AuthenticatedUser, request: LeaveRequestData) -> None:
        if actor.role.upper() == "HR":
            return
        if actor.role.upper() != "MANAGER" or request.manager_employee_id != actor.employee_id:
            raise AuthorizationError("You are not authorized to manage this leave request")

    @staticmethod
    def _require_pending(request: LeaveRequestData) -> None:
        if request.status is not LeaveStatus.PENDING:
            raise ConflictError(
                f"Only pending leave requests can be changed; request is {request.status.value.lower()}"
            )

    @staticmethod
    def _parse_status(value: str) -> LeaveStatus:
        try:
            return LeaveStatus(value.strip().upper())
        except ValueError as exc:
            raise ValidationError("Unsupported leave request status") from exc

    @staticmethod
    def _comment(value: str | None, *, required: bool = False) -> str | None:
        normalized = value.strip() if value else ""
        if required and not normalized:
            raise ValidationError("A rejection reason is required")
        if len(normalized) > 1000:
            raise ValidationError("Decision comments cannot exceed 1000 characters")
        return normalized or None
