from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from app.application.leave.service import LeaveService
from app.application.pending.service import PendingActionCoordinator
from app.core.exceptions import ApplicationError, ValidationError
from app.core.security import AuthenticatedUser
from app.rag.service import PolicyKnowledgeService


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


class EligibilityArguments(DateRangeArguments):
    leave_type: str = Field(min_length=1, max_length=32)


class RequestListArguments(ToolArguments):
    pass


class RequestHistoryArguments(ToolArguments):
    request_id: int = Field(gt=0)


class PolicySearchArguments(ToolArguments):
    question: str = Field(min_length=1, max_length=2000)


class ManagedRequestsArguments(ToolArguments):
    status: str | None = Field(default="PENDING", max_length=24)


class PrepareApplicationArguments(EligibilityArguments):
    reason: str | None = Field(default=None, max_length=1000)


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
    context_update: dict[str, str] = field(default_factory=dict)


class LeaveToolExecutor:
    """Authenticated, deterministic boundary exposed to the Leave Agent."""

    ARGUMENT_MODELS: ClassVar[dict[str, type[ToolArguments]]] = {
        "get_leave_balance": LeaveBalanceArguments,
        "get_holidays": DateRangeArguments,
        "calculate_leave_days": DateRangeArguments,
        "check_leave_eligibility": EligibilityArguments,
        "get_my_leave_requests": RequestListArguments,
        "get_leave_request_history": RequestHistoryArguments,
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
            "get_holidays": "Get configured holidays in a date range.",
            "calculate_leave_days": "Calculate deterministic working leave days in a date range.",
            "check_leave_eligibility": "Check the authenticated employee's eligibility for a leave range.",
            "get_my_leave_requests": "List the authenticated employee's leave requests.",
            "get_leave_request_history": "Get the authorized audit history for one leave request.",
            "search_leave_policy": "Retrieve grounded leave-policy passages and source metadata.",
            "get_managed_leave_requests": "List leave requests the authenticated manager or HR user may manage.",
            "prepare_leave_application": "Validate and prepare an application for explicit user confirmation; never submits it.",
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
        self, tool: str, arguments: dict[str, Any], *, session_id: str
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
            return handler(parsed, session_id=session_id)
        except ApplicationError as exc:
            return self._error(tool, str(exc))
        except Exception:
            # Infrastructure details belong in server logs, never in the model or UI.
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
            "get_holidays": "Checked holiday calendar",
            "calculate_leave_days": "Calculated working leave days",
            "check_leave_eligibility": "Verified leave eligibility",
            "get_my_leave_requests": "Retrieved leave requests",
            "get_leave_request_history": "Retrieved leave request history",
            "search_leave_policy": "Consulted leave policy",
            "get_managed_leave_requests": "Retrieved managed leave requests",
            "prepare_leave_application": "Prepared leave application",
            "prepare_leave_cancellation": "Prepared leave cancellation",
            "prepare_leave_approval": "Prepared leave approval",
            "prepare_leave_rejection": "Prepared leave rejection",
        }.get(tool, "Executed Leave tool")

    @staticmethod
    def _context(arguments: ToolArguments) -> dict[str, str]:
        values = arguments.model_dump(mode="json", exclude_none=True)
        return {
            key: str(values[key])
            for key in ("leave_type", "start_date", "end_date", "reason")
            if key in values
        }

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
            "get_leave_balance", True, data, label, context_update=self._context(arguments)
        )

    def _get_holidays(self, arguments: DateRangeArguments, **_: Any) -> LeaveToolExecution:
        holidays = sorted(self.leave.get_holidays(arguments.start_date, arguments.end_date))
        return LeaveToolExecution(
            "get_holidays",
            True,
            {"holidays": [item.isoformat() for item in holidays]},
            self._base_label("get_holidays"),
            context_update=self._context(arguments),
        )

    def _calculate_leave_days(
        self, arguments: DateRangeArguments, **_: Any
    ) -> LeaveToolExecution:
        days = self.leave.calculate_leave_days(arguments.start_date, arguments.end_date)
        return LeaveToolExecution(
            "calculate_leave_days",
            True,
            {"working_days": _number(days)},
            self._base_label("calculate_leave_days"),
            context_update=self._context(arguments),
        )

    def _check_leave_eligibility(
        self, arguments: EligibilityArguments, **_: Any
    ) -> LeaveToolExecution:
        result = self.leave.check_leave_eligibility(
            self.actor, arguments.leave_type, arguments.start_date, arguments.end_date
        )
        return LeaveToolExecution(
            "check_leave_eligibility",
            True,
            {
                "eligible": result.eligible,
                "leave_type": arguments.leave_type.upper(),
                "start_date": arguments.start_date.isoformat(),
                "end_date": arguments.end_date.isoformat(),
                "requested_days": _number(result.working_days),
                "available_days": _number(result.available_days),
                "reason": result.reason,
            },
            self._base_label("check_leave_eligibility"),
            context_update=self._context(arguments),
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

    def _prepare_leave_application(
        self, arguments: PrepareApplicationArguments, *, session_id: str
    ) -> LeaveToolExecution:
        eligibility = self.leave.check_leave_eligibility(
            self.actor, arguments.leave_type, arguments.start_date, arguments.end_date
        )
        if not eligibility.eligible:
            return LeaveToolExecution(
                "prepare_leave_application",
                False,
                {
                    "eligible": False,
                    "requested_days": _number(eligibility.working_days),
                    "available_days": _number(eligibility.available_days),
                    "reason": eligibility.reason,
                },
                "Leave application was not eligible",
                context_update=self._context(arguments),
            )
        summary = (
            f"Apply for {_number(eligibility.working_days)} working day(s) of "
            f"{arguments.leave_type.title()} leave from {arguments.start_date} to {arguments.end_date}"
        )
        action = self.pending.propose(
            self.actor,
            session_id,
            "apply_leave",
            arguments.model_dump(mode="json"),
            summary,
        )
        return LeaveToolExecution(
            "prepare_leave_application",
            True,
            {"prepared": True, "summary": action.summary},
            self._base_label("prepare_leave_application"),
            pending_summary=action.summary,
            context_update=self._context(arguments),
        )

    def _prepare_leave_cancellation(
        self, arguments: PrepareCancellationArguments, *, session_id: str
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
        self, arguments: PrepareApprovalArguments, *, session_id: str
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
        self, arguments: PrepareRejectionArguments, *, session_id: str
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
