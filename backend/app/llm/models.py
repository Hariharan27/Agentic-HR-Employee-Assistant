from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


ModelTier = Literal["router", "standard", "complex"]
Domain = Literal["leave", "policy", "onboarding", "parking", "general"]
Intent = Literal[
    "policy_question",
    "leave_balance",
    "leave_eligibility",
    "apply_leave",
    "leave_requests",
    "manager_leave_requests",
    "approve_leave_request",
    "reject_leave_request",
    "cancel_leave_request",
    "leave_request_history",
    "calculate_leave_days",
    "holidays",
    "onboarding",
    "parking",
    "general",
]


class RouteDecision(BaseModel):
    domain: Domain
    intent: Intent
    confidence: float = Field(ge=0, le=1)
    leave_type: str | None = None
    start_date: date | None = None
    end_date: date | None = None
    reason: str | None = Field(default=None, max_length=1000)
    request_id: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def align_domain_with_intent(self):
        domains_by_intent = {
            "policy_question": "policy",
            "leave_balance": "leave",
            "leave_eligibility": "leave",
            "apply_leave": "leave",
            "leave_requests": "leave",
            "manager_leave_requests": "leave",
            "approve_leave_request": "leave",
            "reject_leave_request": "leave",
            "cancel_leave_request": "leave",
            "leave_request_history": "leave",
            "calculate_leave_days": "leave",
            "holidays": "leave",
            "onboarding": "onboarding",
            "parking": "parking",
            "general": "general",
        }
        self.domain = domains_by_intent[self.intent]
        return self

    @field_validator("leave_type")
    @classmethod
    def normalize_leave_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper().replace(" ", "_")
        aliases = {
            "CL": "CASUAL",
            "CASUAL_LEAVE": "CASUAL",
            "SL": "SICK",
            "SICK_LEAVE": "SICK",
            "PL": "PRIVILEGE",
            "EL": "PRIVILEGE",
            "EARNED": "PRIVILEGE",
            "EARNED_LEAVE": "PRIVILEGE",
            "PRIVILEGE_LEAVE": "PRIVILEGE",
        }
        result = aliases.get(normalized, normalized)
        if result not in {"CASUAL", "SICK", "PRIVILEGE"}:
            raise ValueError("unsupported leave type")
        return result
