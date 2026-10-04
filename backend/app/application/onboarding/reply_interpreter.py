import json
from typing import Literal

from pydantic import BaseModel, ValidationError as PydanticValidationError

from app.application.notifications.inbound import NotificationDepartment
from app.core.exceptions import LLMServiceError
from app.domain.onboarding.entities import (
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.llm.ports import LLMGateway


class OnboardingTaskUpdate(BaseModel):
    task_type: OnboardingTaskType
    status: Literal[
        OnboardingTaskStatus.IN_PROGRESS,
        OnboardingTaskStatus.COMPLETED,
    ]

class OnboardingReplyDecision(BaseModel):
    updates: list[OnboardingTaskUpdate]


class OnboardingReplyInterpreter:
    """Uses the LLM only to interpret department onboarding replies."""

    def __init__(self, llm: LLMGateway) -> None:
        self.llm = llm

    def validate_decision(
        self,
        *,
        decision: OnboardingReplyDecision,
        allowed_tasks: frozenset[OnboardingTaskType],
    ) -> OnboardingReplyDecision:
        for update in decision.updates:
            if update.task_type not in allowed_tasks:
                raise LLMServiceError(
                    f"The onboarding reply model attempted to update "
                    f"unauthorized task {update.task_type.value}"
                )

        return decision

    def interpret(
        self,
        *,
        department: NotificationDepartment,
        email_body: str,
        allowed_tasks: frozenset[OnboardingTaskType],
    ) -> OnboardingReplyDecision:
        allowed_task_values = sorted(task.value for task in allowed_tasks)

        system = (
            "You interpret department email replies about employee onboarding tasks.\n"
            "Return JSON only.\n"
            "Do not invent task updates that are not clearly supported by the email.\n"
            "Only use task types provided in the allowed task list.\n"
            "Use COMPLETED only when the email clearly says the task is finished or ready.\n"
            "Use IN_PROGRESS when work has started, is being processed, ordered, "
            "configured, or is expected later.\n"
            "If the email does not provide a clear update for a task, omit that task.\n"
            'Return exactly this shape: '
            '{"updates":[{"task_type":"TASK_TYPE","status":"STATUS"}]}'
        )

        user = (
            f"Department: {department.value}\n"
            f"Allowed task types: {json.dumps(allowed_task_values)}\n"
            f"Email body:\n{email_body}"
        )

        raw = self.llm.complete(
            "standard",
            system=system,
            user=user,
            json_mode=True,
        )
        decision = self._parse(raw)

        return self.validate_decision(
            decision=decision,
            allowed_tasks=allowed_tasks,
        )

    @staticmethod
    def _parse(raw: str) -> OnboardingReplyDecision:
        cleaned = raw.strip()

        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.startswith("json"):
                cleaned = cleaned[4:].lstrip()

        last_error: Exception | None = None

        for start, character in enumerate(cleaned):
            if character != "{":
                continue

            try:
                payload, _ = json.JSONDecoder().raw_decode(cleaned[start:])
                return OnboardingReplyDecision.model_validate(payload)
            except (
                ValueError,
                json.JSONDecodeError,
                PydanticValidationError,
            ) as exc:
                last_error = exc

        raise LLMServiceError(
            "The onboarding reply model returned invalid structured output"
        ) from last_error