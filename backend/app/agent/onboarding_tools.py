"""Typed onboarding tools for the Onboarding Agent, over OnboardingService.

Identity and roles come from the authenticated actor. Authorization failures are raised (HTTP 403)
rather than returned to the model. Every candidate value the model supplies in chat must appear in
the employee's current message, so the model cannot invent a name, email, date or manager.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticValidationError

from app.application.onboarding.service import OnboardingService
from app.application.pending.service import PendingActionCoordinator
from app.core.exceptions import ApplicationError, AuthorizationError
from app.core.security import AuthenticatedUser
from app.domain.leave.dates import resolve_leave_dates
from app.domain.onboarding.draft import FIELDS, LABELS, missing_fields, normalize_field
from app.domain.onboarding.entities import TASK_TITLES, OnboardingCandidate

logger = logging.getLogger("app.onboarding_tools")

CREATOR_ROLES = {"MANAGER", "HR"}
REVIEWER_ROLES = {"HR_ADMIN"}


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DraftArguments(ToolArguments):
    name: str | None = Field(default=None, max_length=160)
    email: str | None = Field(default=None, max_length=255)
    designation: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    reporting_manager: str | None = Field(default=None, max_length=160)
    joining_date: str | None = Field(default=None, max_length=80, description="Joining date words as the user wrote them")
    location: str | None = Field(default=None, max_length=120)
    employment_type: str | None = Field(default=None, max_length=40)


class NoArguments(ToolArguments):
    pass


class EmailArguments(ToolArguments):
    email: str = Field(min_length=3, max_length=255)


class PlanIdArguments(ToolArguments):
    plan_id: str = Field(min_length=1, max_length=40)


class StatusArguments(ToolArguments):
    request_id: int | None = Field(default=None, gt=0)
    employee: str | None = Field(default=None, max_length=255, description="Employee name or email as written")


class RequestIdArguments(ToolArguments):
    request_id: int = Field(gt=0)


class ApprovalArguments(ToolArguments):
    request_id: int = Field(gt=0)
    comment: str | None = Field(default=None, max_length=1000)


class RejectionArguments(ToolArguments):
    request_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)


@dataclass(frozen=True, slots=True)
class OnboardingToolExecution:
    tool: str
    ok: bool
    data: dict[str, Any]
    label: str
    sources: list[dict[str, object]] = field(default_factory=list)
    pending_summary: str | None = None
    plan: dict[str, Any] | None = None
    pending_response: str | None = None


def _words(text: str) -> list[str]:
    # Apostrophes split words, so "Priya's" contains "priya" and "O'Brien" matches "O'Brien".
    return [word.strip(".-") for word in re.findall(r"[a-z0-9@._-]+", text.casefold()) if word.strip(".-")]


def stated_in(value: str, text: str) -> bool:
    """Whether every word of the value appears in the user's text."""
    haystack = set(_words(text))
    needed = _words(value)
    return bool(needed) and all(word in haystack for word in needed)


class OnboardingToolExecutor:
    TOOLS: ClassVar[dict[str, tuple[type[ToolArguments], set[str], str, str]]] = {
        # name: (arguments, roles, description, activity label)
        "update_onboarding_draft": (
            DraftArguments, CREATOR_ROLES,
            "Add or change candidate fields the user stated in this message, copied as written "
            "(joining_date as the user's date words). Returns the draft, missing fields and invalid values.",
            "Updated onboarding details",
        ),
        "list_reporting_managers": (
            NoArguments, CREATOR_ROLES | REVIEWER_ROLES,
            "List employees who can be reporting managers.", "Listed reporting managers",
        ),
        "check_employee_exists": (
            EmailArguments, CREATOR_ROLES | REVIEWER_ROLES,
            "Check whether an employee or active onboarding already uses this email.", "Checked for duplicates",
        ),
        "build_onboarding_plan": (
            NoArguments, CREATOR_ROLES,
            "Validate the complete draft and build the onboarding plan (manager, provisioning tasks). "
            "Returns a plan_id.", "Built onboarding plan",
        ),
        "prepare_onboarding": (
            PlanIdArguments, CREATOR_ROLES,
            "Prepare the built onboarding plan for explicit user confirmation; never creates it.",
            "Prepared onboarding request",
        ),
        "get_onboarding_status": (
            StatusArguments, CREATOR_ROLES | REVIEWER_ROLES,
            "Task-by-task status of one onboarding request, by request_id or employee name/email.",
            "Checked onboarding status",
        ),
        "list_onboarding_approvals": (
            NoArguments, REVIEWER_ROLES, "List onboarding requests pending HR administrator approval.",
            "Listed onboarding approvals",
        ),
        "get_onboarding_request": (
            RequestIdArguments, REVIEWER_ROLES, "Details of one onboarding request pending review.",
            "Retrieved onboarding request",
        ),
        "prepare_onboarding_approval": (
            ApprovalArguments, REVIEWER_ROLES,
            "Prepare approval (account activation) of a pending request for confirmation; never approves it.",
            "Prepared onboarding approval",
        ),
        "prepare_onboarding_rejection": (
            RejectionArguments, REVIEWER_ROLES,
            "Prepare rejection of a pending request with the user's reason, for confirmation.",
            "Prepared onboarding rejection",
        ),
    }

    def __init__(
        self,
        *,
        actor: AuthenticatedUser,
        onboarding: OnboardingService,
        pending: PendingActionCoordinator,
    ) -> None:
        self.actor = actor
        self.onboarding = onboarding
        self.pending = pending
        self.turn_text = ""

    def _allowed(self, name: str) -> bool:
        return self.actor.role.upper() in self.TOOLS[name][1]

    def definitions(self) -> list[dict[str, Any]]:
        return [
            {"name": name, "description": spec[2], "arguments_schema": spec[0].model_json_schema()}
            for name, spec in self.TOOLS.items()
            if self._allowed(name)
        ]

    def execute(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        session_id: str,
        active_plan: dict[str, Any] | None = None,
    ) -> OnboardingToolExecution:
        spec = self.TOOLS.get(tool)
        if spec is None:
            return self._error(tool, "Unsupported onboarding tool")
        if not self._allowed(tool):
            raise AuthorizationError("Your role is not permitted to perform this onboarding action")
        if {"employee_id", "user_id", "actor_id", "role"} & set(arguments):
            return self._error(tool, "Identity fields must come from the authenticated session")
        try:
            parsed = spec[0].model_validate(arguments)
        except PydanticValidationError as exc:
            fields = sorted({str(item["loc"][0]) for item in exc.errors() if item.get("loc")})
            return self._error(tool, "Missing or invalid tool arguments: " + ", ".join(fields))
        try:
            return getattr(self, f"_{tool}")(parsed, session_id=session_id, draft=dict(active_plan or {}))
        except AuthorizationError:
            raise
        except ApplicationError as exc:
            return self._error(tool, str(exc))
        except Exception:
            logger.exception("onboarding_tool_failed", extra={"tool": tool, "session_id": session_id})
            return self._error(tool, "The onboarding service could not complete this operation")

    def _ok(self, tool: str, data: dict[str, Any], **extra: Any) -> OnboardingToolExecution:
        return OnboardingToolExecution(tool, True, data, self.TOOLS[tool][3], **extra)

    def _error(self, tool: str, message: str) -> OnboardingToolExecution:
        label = self.TOOLS[tool][3] if tool in self.TOOLS else "Onboarding tool"
        return OnboardingToolExecution(tool, False, {"error": message}, f"{label} failed")

    # ------------------------------------------------------------------ draft

    def apply_fields(
        self, draft: dict[str, str], values: dict[str, str], *, require_stated: bool
    ) -> tuple[dict[str, str], dict[str, str], list[str]]:
        """Validate values into the draft. Returns (draft, invalid, not_stated)."""
        today = self.onboarding.today()
        invalid: dict[str, str] = {}
        not_stated: list[str] = []
        updated = {key: value for key, value in draft.items() if key in FIELDS}
        changed = False
        for name in FIELDS:
            raw = values.get(name)
            if raw is None or not str(raw).strip():
                continue
            raw = str(raw).strip()
            if require_stated and not self._is_stated(name, raw):
                not_stated.append(LABELS[name])
                continue
            value, error = normalize_field(name, raw, today)
            if error is None and name == "reporting_manager":
                value = self.onboarding.resolve_reporting_manager(self.actor, raw)
                if value is None:
                    error = "no single reporting manager matches that name"
            if error:
                invalid[LABELS[name]] = error
                continue
            if updated.get(name) != value:
                updated[name] = value
                changed = True
        if not changed and "plan_id" in draft:
            updated["plan_id"], updated["fingerprint"] = draft["plan_id"], draft.get("fingerprint", "")
        return updated, invalid, not_stated

    def _is_stated(self, name: str, raw: str) -> bool:
        if stated_in(raw, self.turn_text):
            return True
        if name == "joining_date":
            try:
                said = resolve_leave_dates(self.turn_text, self.onboarding.today())
                given = resolve_leave_dates(raw, self.onboarding.today())
            except Exception:  # noqa: BLE001
                return False
            return given.shape == "single" and given.dates[0] in said.dates
        return False

    def draft_view(self, draft: dict[str, str], **extra: Any) -> dict[str, Any]:
        return {
            "draft": {LABELS[name]: draft[name] for name in FIELDS if draft.get(name)},
            "missing": missing_fields(draft),
            **extra,
        }

    def _update_onboarding_draft(self, arguments: DraftArguments, *, draft: dict[str, str], **_: Any):
        updated, invalid, not_stated = self.apply_fields(
            draft, arguments.model_dump(exclude_none=True), require_stated=True
        )
        data = self.draft_view(updated, invalid=invalid, not_stated_by_user=not_stated)
        return self._ok("update_onboarding_draft", data, plan=updated)

    # ------------------------------------------------------------------ plan

    def candidate_from(self, draft: dict[str, str]) -> OnboardingCandidate:
        return OnboardingCandidate(
            name=draft["name"], email=draft["email"], designation=draft["designation"],
            department=draft["department"], reporting_manager=draft["reporting_manager"],
            joining_date=date.fromisoformat(draft["joining_date"]), location=draft["location"],
            employment_type=draft["employment_type"],
        )

    def build_plan(self, draft: dict[str, str]):
        plan = self.onboarding.prepare_plan(self.actor, self.candidate_from(draft))
        fingerprint = self.onboarding.plan_fingerprint(plan)
        return plan, fingerprint

    @staticmethod
    def plan_text(plan) -> str:
        task_lines = "\n".join(f"- {TASK_TITLES[item]}" for item in plan.task_types)
        candidate = plan.candidate
        return (
            "New employee onboarding\n"
            f"Name: {candidate.name}\n"
            f"Email: {candidate.email}\n"
            "Account role: Employee\n"
            f"Designation: {candidate.designation}\n"
            f"Department: {candidate.department}\n"
            f"Manager: {candidate.reporting_manager}\n"
            f"Joining date: {candidate.joining_date}\n"
            f"Location: {candidate.location}\n"
            f"Employment type: {candidate.employment_type}\n\n"
            f"Provisioning requests:\n{task_lines}"
        )

    def _build_onboarding_plan(self, arguments: NoArguments, *, draft: dict[str, str], **_: Any):
        missing = missing_fields(draft)
        if missing:
            return self._error("build_onboarding_plan", "Missing required onboarding fields: " + ", ".join(missing))
        plan, fingerprint = self.build_plan(draft)
        plan_id = f"op_{fingerprint[:10]}"
        updated = {**{key: draft[key] for key in FIELDS}, "plan_id": plan_id, "fingerprint": fingerprint}
        data = {
            "plan_id": plan_id,
            "summary": self.plan_text(plan),
            "provisioning_tasks": [TASK_TITLES[item] for item in plan.task_types],
        }
        return self._ok("build_onboarding_plan", data, plan=updated)

    def propose(self, draft: dict[str, str], session_id: str) -> tuple[str, str]:
        """Rebuild the plan from the draft and store the pending confirmation. Returns (summary, reply)."""
        plan, _ = self.build_plan(draft)
        arguments = {
            "name": plan.candidate.name,
            "email": plan.candidate.email,
            "designation": plan.candidate.designation,
            "department": plan.candidate.department,
            "reporting_manager": plan.candidate.reporting_manager,
            "joining_date": plan.candidate.joining_date.isoformat(),
            "location": plan.candidate.location,
            "employment_type": plan.candidate.employment_type,
        }
        summary = (
            f"Create onboarding for {plan.candidate.name} ({plan.candidate.designation}), "
            f"joining {plan.candidate.joining_date}, with {len(plan.task_types)} provisioning requests"
        )
        action = self.pending.propose(self.actor, session_id, "create_onboarding", arguments, summary)
        return action.summary, f"{self.plan_text(plan)}\n\nReply yes to confirm or cancel."

    def _prepare_onboarding(self, arguments: PlanIdArguments, *, session_id: str, draft: dict[str, str], **_: Any):
        if draft.get("plan_id") != arguments.plan_id:
            return self._error("prepare_onboarding", "No built onboarding plan has that plan_id; build the plan first")
        _, fingerprint = self.build_plan(draft)
        if fingerprint != draft.get("fingerprint"):
            return self._error("prepare_onboarding", "The onboarding details changed; build the plan again")
        summary, reply = self.propose(draft, session_id)
        return self._ok(
            "prepare_onboarding", {"prepared": True, "summary": summary},
            pending_summary=summary, pending_response=reply, plan=draft,
        )

    # ------------------------------------------------------------------ lookups

    def _list_reporting_managers(self, arguments: NoArguments, **_: Any):
        managers = self.onboarding.list_reporting_managers(self.actor)
        return self._ok("list_reporting_managers", {"managers": [
            {"name": item.name, "designation": item.designation, "department": item.department} for item in managers
        ]})

    def _check_employee_exists(self, arguments: EmailArguments, **_: Any):
        exists = self.onboarding.check_employee_exists(self.actor, arguments.email)
        return self._ok("check_employee_exists", {"email": arguments.email.casefold(), "exists": exists})

    @staticmethod
    def _request(item) -> dict[str, Any]:
        return {
            "request_id": item.id,
            "name": item.candidate.name,
            "designation": item.candidate.designation,
            "joining_date": item.candidate.joining_date.isoformat(),
            "status": item.status.value,
            "completed_tasks": item.completed_tasks,
            "total_tasks": item.total_tasks,
            "tasks": [{"title": task.title, "status": task.status.value} for task in item.tasks],
        }

    def _get_onboarding_status(self, arguments: StatusArguments, **_: Any):
        if arguments.request_id is not None:
            request = self.onboarding.get_onboarding_status(self.actor, arguments.request_id)
        elif arguments.employee and stated_in(arguments.employee, self.turn_text):
            request = self.onboarding.find_onboarding_status(self.actor, arguments.employee)
        else:
            return self._error(
                "get_onboarding_status",
                "Ask for the employee name, email, or onboarding request ID (it must come from the user)",
            )
        return self._ok("get_onboarding_status", self._request(request))

    def _list_onboarding_approvals(self, arguments: NoArguments, **_: Any):
        requests = self.onboarding.get_pending_approvals(self.actor)
        return self._ok("list_onboarding_approvals", {"requests": [self._request(item) for item in requests[:20]]})

    def _get_onboarding_request(self, arguments: RequestIdArguments, **_: Any):
        return self._ok("get_onboarding_request", self._request(self.onboarding.prepare_review(self.actor, arguments.request_id)))

    def _prepare_onboarding_approval(self, arguments: ApprovalArguments, *, session_id: str, **_: Any):
        request = self.onboarding.prepare_review(self.actor, arguments.request_id)
        summary = f"Approve onboarding request #{request.id} for {request.candidate.name}"
        action = self.pending.propose(
            self.actor, session_id, "approve_onboarding",
            {"request_id": request.id, "comment": arguments.comment}, summary,
        )
        return self._ok("prepare_onboarding_approval", {"prepared": True, "summary": action.summary},
                        pending_summary=action.summary)

    def _prepare_onboarding_rejection(self, arguments: RejectionArguments, *, session_id: str, **_: Any):
        request = self.onboarding.prepare_review(self.actor, arguments.request_id)
        summary = f"Reject onboarding request #{request.id} for {request.candidate.name}: {arguments.reason}"
        action = self.pending.propose(
            self.actor, session_id, "reject_onboarding",
            {"request_id": request.id, "reason": arguments.reason}, summary,
        )
        return self._ok("prepare_onboarding_rejection", {"prepared": True, "summary": action.summary},
                        pending_summary=action.summary)
