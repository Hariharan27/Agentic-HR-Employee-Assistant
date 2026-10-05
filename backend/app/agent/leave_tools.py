from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from app.application.leave.service import LeaveService
from app.application.pending.service import PendingActionCoordinator
from app.core.exceptions import ApplicationError, ValidationError
from app.core.security import AuthenticatedUser
from app.domain.leave.dates import MAX_RESOLVED_DAYS, describe_day, resolve_leave_dates
from app.domain.leave.entities import LeaveType
from app.domain.leave.plan import stored_plan_inputs
from app.domain.leave.policy_rules import POLICY_DOCUMENT, rules_for
from app.rag.service import PolicyKnowledgeService

logger = logging.getLogger("app.leave_tools")


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


class ToolArguments(BaseModel):
    """Base schema deliberately rejects identity and every undeclared argument."""

    model_config = ConfigDict(extra="forbid")


class LeaveBalanceArguments(ToolArguments):
    leave_type: str | None = Field(default=None, max_length=32)


class DateRangeArguments(ToolArguments):
    start_date: date
    end_date: date


class ResolveDatesArguments(ToolArguments):
    text: str = Field(min_length=1, max_length=300)


class BuildPlanArguments(ToolArguments):
    leave_type: str = Field(min_length=1, max_length=32)
    dates: list[date] = Field(min_length=1, max_length=MAX_RESOLVED_DAYS)
    reason: str | None = Field(default=None, max_length=1000)
    split_with: str | None = Field(
        default=None,
        max_length=32,
        description="Second leave type for the days the first type cannot cover; only after the employee agrees.",
    )


class RequestListArguments(ToolArguments):
    pass


class RequestHistoryArguments(ToolArguments):
    request_id: int = Field(gt=0)


class LeaveRulesArguments(ToolArguments):
    leave_type: str | None = Field(default=None, max_length=32)


class PolicySearchArguments(ToolArguments):
    question: str = Field(min_length=1, max_length=2000)


class ManagedRequestsArguments(ToolArguments):
    status: str | None = Field(default="PENDING", max_length=24)


class ShiftPlanArguments(ToolArguments):
    plan_id: str = Field(min_length=1, max_length=40)
    shift_days: int = Field(
        ge=-62, le=62,
        description="Calendar days to move every date of the active plan: 7 = same days next week, -7 = previous week.",
    )


class PrepareApplicationArguments(ToolArguments):
    plan_id: str = Field(min_length=1, max_length=40)


class PrepareCancellationArguments(ToolArguments):
    request_id: int = Field(gt=0)
    reason: str | None = Field(default=None, max_length=1000)


class PrepareApprovalArguments(ToolArguments):
    request_id: int = Field(gt=0)
    comment: str | None = Field(default=None, max_length=1000)


class PrepareRejectionArguments(ToolArguments):
    request_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


@dataclass(frozen=True, slots=True)
class LeaveToolExecution:
    tool: str
    ok: bool
    data: dict[str, Any]
    label: str
    sources: list[dict[str, object]] = field(default_factory=list)
    pending_summary: str | None = None
    plan: dict[str, Any] | None = None


class LeaveToolExecutor:
    """Authenticated, deterministic boundary exposed to the Leave Agent."""

    ARGUMENT_MODELS: ClassVar[dict[str, type[ToolArguments]]] = {
        "get_leave_balance": LeaveBalanceArguments,
        "resolve_dates": ResolveDatesArguments,
        "build_leave_plan": BuildPlanArguments,
        "shift_leave_plan": ShiftPlanArguments,
        "get_holidays": DateRangeArguments,
        "calculate_leave_days": DateRangeArguments,
        "get_my_leave_requests": RequestListArguments,
        "get_leave_request_history": RequestHistoryArguments,
        "get_leave_rules": LeaveRulesArguments,
        "search_leave_policy": PolicySearchArguments,
        "get_managed_leave_requests": ManagedRequestsArguments,
        "prepare_leave_application": PrepareApplicationArguments,
        "prepare_leave_cancellation": PrepareCancellationArguments,
        "prepare_leave_approval": PrepareApprovalArguments,
        "prepare_leave_rejection": PrepareRejectionArguments,
    }

    def __init__(
        self,
        *,
        actor: AuthenticatedUser,
        leave: LeaveService,
        pending: PendingActionCoordinator,
        policies: PolicyKnowledgeService,
    ) -> None:
        self.actor = actor
        self.leave = leave
        self.pending = pending
        self.policies = policies

    @classmethod
    def definitions(cls) -> list[dict[str, Any]]:
        descriptions = {
            "get_leave_balance": "Get the authenticated employee's actual leave balance.",
            "resolve_dates": (
                "Turn the employee's date words, copied as written (e.g. 'Tuesday and Sunday', "
                "'5th to 7th October', '3 days from 12 Oct'), into exact dates. Returns whether "
                "they are separate days or one range, each date's weekday, and weekend/holiday flags."
            ),
            "build_leave_plan": (
                "Validate a leave request for explicit dates (from resolve_dates) and one leave type. "
                "Returns the plan: working days per segment, weekends/holidays not counted, balance "
                "before/after, eligibility and problems. The newest plan becomes the active plan."
            ),
            "shift_leave_plan": (
                "Move every date of the active plan by a number of calendar days (e.g. 'same leave "
                "next week' = 7) keeping its leave type, split and reason, and re-check it. Returns the new plan."
            ),
            "get_holidays": "Get configured holidays in a date range.",
            "calculate_leave_days": "Calculate deterministic working leave days in a date range.",
            "get_my_leave_requests": "List the authenticated employee's leave requests.",
            "get_leave_request_history": "Get the authorized audit history for one leave request.",
            "get_leave_rules": (
                "Get the leave policy's stated rules (entitlement, credit, carry forward, lapse, "
                "encashment, notice period, approval, holidays) for one leave type or all, with page sources."
            ),
            "search_leave_policy": "Retrieve grounded leave-policy passages and source metadata for anything get_leave_rules does not cover.",
            "get_managed_leave_requests": "List leave requests the authenticated manager or HR user may manage.",
            "prepare_leave_application": "Prepare the active, eligible leave plan (by plan_id) for explicit user confirmation; never submits it.",
            "prepare_leave_cancellation": "Validate and prepare cancellation for explicit confirmation; never cancels it.",
            "prepare_leave_approval": "Validate and prepare manager approval for explicit confirmation; never approves it.",
            "prepare_leave_rejection": "Validate and prepare manager rejection for explicit confirmation; never rejects it.",
        }
        return [
            {
                "name": name,
                "description": descriptions[name],
                "arguments_schema": model.model_json_schema(),
            }
            for name, model in cls.ARGUMENT_MODELS.items()
        ]

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        active_plan: dict[str, Any] | None = None,
    ) -> LeaveToolExecution:
        model = self.ARGUMENT_MODELS.get(tool)
        if model is None:
            return self._error(tool, "Unsupported Leave tool")
        identity_fields = {
            "employee_id", "user_id", "actor_id", "username", "email", "employee_email"
        }
        supplied_identity = sorted(identity_fields.intersection(arguments))
        if supplied_identity:
            return self._error(
                tool,
                "Identity fields must come from the authenticated session, not tool arguments",
            )
        try:
            parsed = model.model_validate(arguments)
        except PydanticValidationError as exc:
            return self._error(tool, self._validation_message(exc))

        try:
            handler = getattr(self, f"_{tool}")
            return handler(parsed, session_id=session_id, active_plan=active_plan)
        except ApplicationError as exc:
            return self._error(tool, str(exc))
        except Exception:
            # Infrastructure details belong in server logs, never in the model or UI.
            logger.exception("leave_tool_failed", extra={"tool": tool, "session_id": session_id})
            return self._error(tool, "The Leave service could not complete this operation")

    @staticmethod
    def _validation_message(exc: PydanticValidationError) -> str:
        fields = sorted({str(item["loc"][0]) for item in exc.errors() if item.get("loc")})
        if not fields:
            return "The tool arguments were invalid"
        return "Missing or invalid tool arguments: " + ", ".join(fields)

    @staticmethod
    def _error(tool: str, message: str) -> LeaveToolExecution:
        return LeaveToolExecution(
            tool=tool,
            ok=False,
            data={"error": message},
            label=f"{LeaveToolExecutor._base_label(tool)} failed",
        )

    @staticmethod
    def _base_label(tool: str) -> str:
        return {
            "get_leave_balance": "Checked leave balance",
            "resolve_dates": "Resolved leave dates",
            "build_leave_plan": "Checked leave eligibility",
            "shift_leave_plan": "Moved leave plan dates",
            "get_holidays": "Checked holiday calendar",
            "calculate_leave_days": "Calculated working leave days",
            "get_my_leave_requests": "Retrieved leave requests",
            "get_leave_request_history": "Retrieved leave request history",
            "get_leave_rules": "Checked leave rules",
            "search_leave_policy": "Consulted leave policy",
            "get_managed_leave_requests": "Retrieved managed leave requests",
            "prepare_leave_application": "Prepared leave application",
            "prepare_leave_cancellation": "Prepared leave cancellation",
            "prepare_leave_approval": "Prepared leave approval",
            "prepare_leave_rejection": "Prepared leave rejection",
        }.get(tool, "Executed Leave tool")

    def _get_leave_balance(
        self, arguments: LeaveBalanceArguments, **_: Any
    ) -> LeaveToolExecution:
        balances = self.leave.get_leave_balance(self.actor, arguments.leave_type)
        data = {
            "balances": [
                {
                    "leave_type": item.leave_type.value,
                    "total_days": _number(item.total_days),
                    "used_days": _number(item.used_days),
                    "pending_days": _number(item.pending_days),
                    "available_days": _number(item.available_days),
                    "carry_forward_limit_days": _number(item.carry_forward_limit_days),
                }
                for item in balances
            ]
        }
        label = self._base_label("get_leave_balance")
        if arguments.leave_type:
            label = f"Checked {arguments.leave_type.title()} Leave balance"
        return LeaveToolExecution(
            "get_leave_balance", True, data, label
        )

    def _get_holidays(self, arguments: DateRangeArguments, **_: Any) -> LeaveToolExecution:
        holidays = self.leave.get_holidays(arguments.start_date, arguments.end_date, self.actor)
        return LeaveToolExecution(
            "get_holidays",
            True,
            {
                "holidays": [item.isoformat() for item in sorted(holidays)],
                "names": {item.isoformat(): holidays[item] for item in sorted(holidays)},
                "calendar": self.leave.holiday_region(self.actor),
            },
            self._base_label("get_holidays"),
        )

    def _calculate_leave_days(
        self, arguments: DateRangeArguments, **_: Any
    ) -> LeaveToolExecution:
        days = self.leave.calculate_leave_days(arguments.start_date, arguments.end_date, self.actor)
        return LeaveToolExecution(
            "calculate_leave_days",
            True,
            {"working_days": _number(days)},
            self._base_label("calculate_leave_days"),
        )

    def _resolve_dates(self, arguments: ResolveDatesArguments, **_: Any) -> LeaveToolExecution:
        today = self.leave.today()
        window_end = date(today.year + 1, 12, 31)
        holidays = self.leave.get_holidays(date(today.year, 1, 1), window_end, self.actor)
        resolution = resolve_leave_dates(arguments.text, today, holidays)
        data: dict[str, Any] = {
            "today": today.isoformat(),
            "shape": resolution.shape,
            "dates": [describe_day(item, holidays) for item in resolution.dates],
        }
        if resolution.ambiguous:
            data["question"] = resolution.question
        return LeaveToolExecution("resolve_dates", True, data, self._base_label("resolve_dates"))

    def _build_leave_plan(self, arguments: BuildPlanArguments, **_: Any) -> LeaveToolExecution:
        plan = self.leave.build_leave_plan(
            self.actor, arguments.leave_type, arguments.dates, arguments.reason, arguments.split_with
        )
        data = plan.to_dict()
        return LeaveToolExecution(
            "build_leave_plan", True, data, self._base_label("build_leave_plan"), plan=data
        )

    def _get_my_leave_requests(
        self, arguments: RequestListArguments, **_: Any
    ) -> LeaveToolExecution:
        requests = self.leave.get_my_leave_requests(self.actor)
        return LeaveToolExecution(
            "get_my_leave_requests",
            True,
            {"requests": [self._request(item) for item in requests[:20]]},
            self._base_label("get_my_leave_requests"),
        )

    def _get_managed_leave_requests(
        self, arguments: ManagedRequestsArguments, **_: Any
    ) -> LeaveToolExecution:
        requests = self.leave.get_managed_leave_requests(self.actor, arguments.status)
        return LeaveToolExecution(
            "get_managed_leave_requests",
            True,
            {"requests": [self._request(item) for item in requests[:30]]},
            self._base_label("get_managed_leave_requests"),
        )

    def _get_leave_request_history(
        self, arguments: RequestHistoryArguments, **_: Any
    ) -> LeaveToolExecution:
        events = self.leave.get_leave_request_history(self.actor, arguments.request_id)
        return LeaveToolExecution(
            "get_leave_request_history",
            True,
            {
                "request_id": arguments.request_id,
                "events": [
                    {
                        "from_status": item.from_status.value if item.from_status else None,
                        "to_status": item.to_status.value,
                        "actor_user_id": item.actor_user_id,
                        "comment": item.comment,
                        "created_at": item.created_at.isoformat() if item.created_at else None,
                    }
                    for item in events
                ],
            },
            self._base_label("get_leave_request_history"),
        )

    def _get_leave_rules(self, arguments: LeaveRulesArguments, **_: Any) -> LeaveToolExecution:
        leave_type = None
        if arguments.leave_type:
            normalized = arguments.leave_type.strip().upper().replace(" ", "_")
            if normalized not in {"PL", "PRIVILEGE", "PRIVILEGE_LEAVE"}:
                try:
                    leave_type = LeaveType.parse(arguments.leave_type)
                except ValueError:
                    return self._error("get_leave_rules", "Unsupported leave type. Supported values: CASUAL, SICK, EARNED")
        rules = rules_for(leave_type)
        pages = sorted({rule.page for rule in rules})
        sources = [
            {"document": POLICY_DOCUMENT, "page": page, "section": None, "category": "leave", "score": 1.0}
            for page in pages
        ]
        return LeaveToolExecution(
            "get_leave_rules",
            True,
            {"leave_type": leave_type.value if leave_type else None, "rules": [rule.to_dict() for rule in rules]},
            self._base_label("get_leave_rules"),
            sources=sources,
        )

    def _search_leave_policy(
        self, arguments: PolicySearchArguments, **_: Any
    ) -> LeaveToolExecution:
        context = self.policies.search(arguments.question)
        return LeaveToolExecution(
            "search_leave_policy",
            True,
            {"policy_context": context.text},
            self._base_label("search_leave_policy"),
            sources=context.sources,
        )

    def _shift_leave_plan(
        self, arguments: ShiftPlanArguments, *, active_plan: dict[str, Any] | None = None, **_: Any
    ) -> LeaveToolExecution:
        inputs = stored_plan_inputs(active_plan)
        if inputs is None or inputs.plan_id != arguments.plan_id:
            return self._error("shift_leave_plan", "No active leave plan has that plan_id; build a leave plan first")
        if arguments.shift_days == 0:
            return self._error("shift_leave_plan", "shift_days must not be 0")
        shifted = [day + timedelta(days=arguments.shift_days) for day in inputs.dates]
        plan = self.leave.build_leave_plan(
            self.actor, inputs.leave_type, shifted, inputs.reason, inputs.split_with
        )
        data = plan.to_dict()
        return LeaveToolExecution("shift_leave_plan", True, data, self._base_label("shift_leave_plan"), plan=data)

    def _prepare_leave_application(
        self,
        arguments: PrepareApplicationArguments,
        *,
        session_id: str,
        active_plan: dict[str, Any] | None = None,
        **_: Any,
    ) -> LeaveToolExecution:
        inputs = stored_plan_inputs(active_plan)
        if inputs is None or inputs.plan_id != arguments.plan_id:
            return self._error(
                "prepare_leave_application",
                "No active leave plan has that plan_id; build a leave plan for the requested dates first",
            )
        if inputs.expires_at <= self.leave.now():
            return self._error(
                "prepare_leave_application",
                "The leave plan expired; build it again for the requested dates",
            )
        plan = self.leave.build_leave_plan(
            self.actor, inputs.leave_type, inputs.dates, inputs.reason, inputs.split_with
        )
        if plan.compute_fingerprint() != inputs.fingerprint:
            data = plan.to_dict()
            return LeaveToolExecution(
                "prepare_leave_application",
                False,
                {"error": "The leave details changed; review the updated plan with the employee", "updated_plan": data},
                "Leave plan changed",
                plan=data,
            )
        if not plan.eligible:
            return LeaveToolExecution(
                "prepare_leave_application",
                False,
                {"eligible": False, "reason": "; ".join(plan.problems)},
                "Leave application was not eligible",
            )
        summary = plan.confirmation_summary()
        action = self.pending.propose(
            self.actor,
            session_id,
            "apply_leave_plan",
            {
                "leave_type": plan.leave_type.value,
                "dates": [item.isoformat() for item in plan.requested_dates],
                "reason": plan.reason,
                "split_with": plan.split_with.value if plan.split_with else None,
                "fingerprint": inputs.fingerprint,
            },
            summary,
        )
        return LeaveToolExecution(
            "prepare_leave_application",
            True,
            {"prepared": True, "summary": action.summary},
            self._base_label("prepare_leave_application"),
            pending_summary=action.summary,
        )

    def _prepare_leave_cancellation(
        self, arguments: PrepareCancellationArguments, *, session_id: str, **_: Any
    ) -> LeaveToolExecution:
        request = self.leave.prepare_leave_cancellation(self.actor, arguments.request_id)
        summary = f"Cancel your pending leave request #{request.id}"
        action = self.pending.propose(
            self.actor,
            session_id,
            "cancel_leave_request",
            arguments.model_dump(mode="json"),
            summary,
        )
        return LeaveToolExecution(
            "prepare_leave_cancellation",
            True,
            {"prepared": True, "request": self._request(request), "summary": action.summary},
            self._base_label("prepare_leave_cancellation"),
            pending_summary=action.summary,
        )

    def _prepare_leave_approval(
        self, arguments: PrepareApprovalArguments, *, session_id: str, **_: Any
    ) -> LeaveToolExecution:
        request = self.leave.prepare_leave_decision(self.actor, arguments.request_id)
        subject = request.employee_name or request.employee_code or f"employee #{request.employee_id}"
        summary = f"Approve leave request #{request.id} for {subject}"
        action = self.pending.propose(
            self.actor,
            session_id,
            "approve_leave_request",
            arguments.model_dump(mode="json"),
            summary,
        )
        return LeaveToolExecution(
            "prepare_leave_approval",
            True,
            {"prepared": True, "request": self._request(request), "summary": action.summary},
            self._base_label("prepare_leave_approval"),
            pending_summary=action.summary,
        )

    def _prepare_leave_rejection(
        self, arguments: PrepareRejectionArguments, *, session_id: str, **_: Any
    ) -> LeaveToolExecution:
        request = self.leave.prepare_leave_decision(self.actor, arguments.request_id)
        summary = f"Reject leave request #{request.id}: {arguments.reason}"
        action = self.pending.propose(
            self.actor,
            session_id,
            "reject_leave_request",
            arguments.model_dump(mode="json"),
            summary,
        )
        return LeaveToolExecution(
            "prepare_leave_rejection",
            True,
            {"prepared": True, "request": self._request(request), "summary": action.summary},
            self._base_label("prepare_leave_rejection"),
            pending_summary=action.summary,
        )

    @staticmethod
    def _request(item: Any) -> dict[str, Any]:
        return {
            "request_id": item.id,
            "employee_name": item.employee_name,
            "employee_code": item.employee_code,
            "leave_type": item.leave_type.value,
            "start_date": item.start_date.isoformat(),
            "end_date": item.end_date.isoformat(),
            "working_days": _number(item.working_days),
            "status": item.status.value,
            "reason": item.reason,
        }
