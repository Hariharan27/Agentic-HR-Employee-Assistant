from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

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
from app.domain.leave.holidays import DEFAULT_REGION, region_for_location
from app.domain.leave.plan import ExcludedDay, LeavePlan, PlanSegment, plan_ttl
from app.domain.leave.rules import calculate_working_days, validate_date_range

from app.domain.leave.policy_rules import SICK_BACKDATE_DAYS

MAX_PLAN_DAYS = 62


class LeaveService:
    """Deterministic leave use cases. Employee identity always comes from trusted auth context."""

    def __init__(
        self,
        repository: LeaveRepository,
        *,
        today: Callable[[], date] | None = None,
        now: Callable[[], datetime] | None = None,
    ):
        self.repository = repository
        self.today = today or date.today
        self.now = now or (lambda: datetime.now(UTC))

    @staticmethod
    def _parse_leave_type(value: str) -> LeaveType:
        if value.strip().upper().replace(" ", "_") in {"PL", "PRIVILEGE", "PRIVILEGE_LEAVE"}:
            raise ValidationError(
                "Privilege Leave (PL) is a separate legacy balance kept after the EL conversion and is "
                "not managed in PeopleDesk; check or apply PL in iAssistant. PeopleDesk handles "
                "CASUAL, SICK and EARNED leave"
            )
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

    def holiday_region(self, actor: AuthenticatedUser | None) -> str:
        if actor is None:
            return DEFAULT_REGION
        return region_for_location(self.repository.get_employee_location(actor.employee_id))

    def get_holidays(
        self, start_date: date, end_date: date, actor: AuthenticatedUser | None = None
    ) -> dict[date, str]:
        """Holidays (date -> name) in the employee's regional calendar."""
        validate_date_range(start_date, end_date)
        return self.repository.get_holidays(start_date, end_date, self.holiday_region(actor))

    def calculate_leave_days(
        self, start_date: date, end_date: date, actor: AuthenticatedUser | None = None
    ) -> Decimal:
        return calculate_working_days(
            start_date, end_date, set(self.get_holidays(start_date, end_date, actor))
        )

    def check_leave_eligibility(self, actor: AuthenticatedUser, leave_type: str,
                                start_date: date, end_date: date) -> LeaveEligibility:
        parsed = self._parse_leave_type(leave_type)
        working_days = self.calculate_leave_days(start_date, end_date, actor)
        if working_days == 0:
            return LeaveEligibility(False, working_days, Decimal("0"), "The selected range has no working days")
        balances = self.get_leave_balance(actor, parsed.value)
        available = balances[0].available_days
        if self.repository.has_overlapping_request(actor.employee_id, start_date, end_date):
            return LeaveEligibility(False, working_days, available, "An active leave request overlaps this date range")
        if working_days > available:
            return LeaveEligibility(False, working_days, available, "Insufficient available leave balance")
        return LeaveEligibility(True, working_days, available)

    def build_leave_plan(
        self,
        actor: AuthenticatedUser,
        leave_type: str,
        dates: list[date] | tuple[date, ...],
        reason: str | None = None,
        split_with: str | None = None,
    ) -> LeavePlan:
        """Validate explicit leave dates and group them into contiguous request segments.

        Weekends and holidays inside the requested dates are excluded and reported, separate days
        stay separate requests, and every business rule is evaluated here, never by the model.
        """
        parsed = self._parse_leave_type(leave_type)
        requested = tuple(sorted(set(dates)))
        if not requested:
            raise ValidationError("At least one leave date is required")
        if (requested[-1] - requested[0]).days + 1 > MAX_PLAN_DAYS:
            raise ValidationError(f"A leave plan can cover at most {MAX_PLAN_DAYS} calendar days")
        holidays = self.repository.get_holidays(requested[0], requested[-1], self.holiday_region(actor))

        def working(day: date) -> bool:
            return day.weekday() < 5 and day not in holidays

        excluded = tuple(
            ExcludedDay(day, "holiday", holidays[day]) if day in holidays else ExcludedDay(day, "weekly off")
            for day in requested if not working(day)
        )
        working_dates = [day for day in requested if working(day)]
        requested_set = set(requested)

        balances = {item.leave_type: item.available_days for item in self.get_leave_balance(actor)}
        if parsed not in balances:
            raise NotFoundError("No leave balance was found for the requested leave type")
        available = balances[parsed]
        secondary = self._parse_leave_type(split_with) if split_with else None
        if secondary is parsed:
            raise ValidationError("A split must combine two different leave types")
        if secondary is not None and secondary not in balances:
            raise NotFoundError(f"No {secondary.value.title()} leave balance was found")
        # Requested type first, in date order; the remaining days use the agreed second type.
        primary_count = len(working_dates) if secondary is None else int(min(available, len(working_dates)))
        assigned = [
            (day, parsed if index < primary_count else secondary)
            for index, day in enumerate(working_dates)
        ]

        groups: list[list[tuple[date, LeaveType]]] = []
        for day, kind in assigned:
            if groups and groups[-1][-1][1] is kind:
                previous = groups[-1][-1][0]
                gap = [previous + timedelta(days=offset) for offset in range(1, (day - previous).days)]
                if all(item in requested_set or not working(item) for item in gap):
                    groups[-1].append((day, kind))
                    continue
            groups.append([(day, kind)])
        segments = tuple(
            PlanSegment(group[0][1], group[0][0], group[-1][0], Decimal(len(group))) for group in groups
        )

        problems: list[str] = []
        today = self.today()
        earliest = today - timedelta(days=SICK_BACKDATE_DAYS) if parsed is LeaveType.SICK else today
        if working_dates and working_dates[0] < earliest:
            problems.append(
                f"{parsed.value.title()} leave cannot start before {earliest.isoformat()}"
                if parsed is LeaveType.SICK
                else f"{parsed.value.title()} leave cannot be applied for past dates ({working_dates[0].isoformat()})"
            )
        later_years = sorted({day.year for day in working_dates if day.year > today.year})
        if later_years:
            problems.append(
                "Leave entitlements follow the calendar year, so dates in "
                + ", ".join(str(year) for year in later_years)
                + " can be applied once that year's leave is credited"
            )
        if not segments:
            problems.append("The selected dates have no working days")
        for segment in segments:
            if self.repository.has_overlapping_request(actor.employee_id, segment.start_date, segment.end_date):
                problems.append(
                    f"An active leave request overlaps {segment.start_date.isoformat()}"
                    + ("" if segment.start_date == segment.end_date else f" to {segment.end_date.isoformat()}")
                )
        days_by_type: dict[LeaveType, Decimal] = {}
        for segment in segments:
            days_by_type[segment.leave_type] = days_by_type.get(segment.leave_type, Decimal("0")) + segment.working_days
        for kind, needed in days_by_type.items():
            if needed > balances[kind]:
                problems.append(
                    f"Insufficient {kind.value.title()} balance: needs {needed.normalize():f} day(s), "
                    f"{balances[kind].normalize():f} available"
                )
        split_options: tuple[tuple[LeaveType, Decimal], ...] = ()
        primary_needed = days_by_type.get(parsed, Decimal("0"))
        if secondary is None and primary_needed > available:
            shortfall = primary_needed - available
            split_options = tuple(
                (kind, balances[kind])
                for kind in LeaveType
                if kind is not parsed and kind in balances and balances[kind] >= shortfall
            )
        normalized_reason = reason.strip() if reason and reason.strip() else None
        plan = LeavePlan(
            leave_type=parsed,
            requested_dates=requested,
            segments=segments,
            excluded_days=excluded,
            available_before=available,
            problems=tuple(problems),
            reason=normalized_reason,
            split_with=secondary,
            secondary_available=balances[secondary] if secondary is not None else None,
            split_options=split_options,
        )
        return replace(
            plan,
            plan_id=f"lp_{uuid4().hex[:10]}",
            expires_at=self.now() + plan_ttl(),
            fingerprint=plan.compute_fingerprint(),
        )

    def stage_leave_plan(
        self,
        actor: AuthenticatedUser,
        leave_type: str,
        dates: list[date] | tuple[date, ...],
        expected_fingerprint: str,
        reason: str | None = None,
        split_with: str | None = None,
    ) -> list[LeaveRequestData]:
        """Rebuild the confirmed plan, require it unchanged, and stage one request per segment."""
        plan = self.build_leave_plan(actor, leave_type, dates, reason, split_with)
        if plan.fingerprint != expected_fingerprint:
            raise ConflictError(
                "The leave details changed since this confirmation was prepared "
                f"({plan.summary()}). Cancel it and ask again for an updated plan"
            )
        if not plan.eligible:
            raise ValidationError("; ".join(plan.problems) or "The leave plan is not eligible")
        return [
            self.stage_leave_application(actor, segment.leave_type.value, segment.start_date,
                                         segment.end_date, plan.reason)
            for segment in plan.segments
        ]

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
        working_days = calculate_working_days(
            start_date, end_date,
            set(self.repository.get_holidays(start_date, end_date, self.holiday_region(actor))),
        )
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
