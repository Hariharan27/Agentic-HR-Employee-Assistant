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
    "start_onboarding",
    "onboarding_status",
    "onboarding_approvals",
    "approve_onboarding",
    "reject_onboarding",
    "parking_vehicle",
    "register_vehicle",
    "parking_availability",
    "reserve_parking",
    "parking_reservations",
    "cancel_parking",
    "join_parking_waitlist",
    "parking_admin_reservations",
    "check_in_parking",
    "admin_cancel_parking",
    "mark_parking_no_show",
    "override_parking_no_show",
    "complete_parking",
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
    employee_name: str | None = Field(default=None, max_length=160)
    employee_email: str | None = Field(default=None, max_length=255)
    designation: str | None = Field(default=None, max_length=120)
    department: str | None = Field(default=None, max_length=120)
    reporting_manager: str | None = Field(default=None, max_length=160)
    joining_date: date | None = None
    location: str | None = Field(default=None, max_length=120)
    employment_type: str | None = Field(default=None, max_length=40)
    parking_date: date | None = None
    vehicle_registration: str | None = Field(default=None, max_length=32)
    vehicle_type: str | None = Field(default=None, max_length=24)
    vehicle_make_model: str | None = Field(default=None, max_length=120)

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
            "start_onboarding": "onboarding",
            "onboarding_status": "onboarding",
            "onboarding_approvals": "onboarding",
            "approve_onboarding": "onboarding",
            "reject_onboarding": "onboarding",
            "parking_vehicle": "parking",
            "register_vehicle": "parking",
            "parking_availability": "parking",
            "reserve_parking": "parking",
            "parking_reservations": "parking",
            "cancel_parking": "parking",
            "join_parking_waitlist": "parking",
            "parking_admin_reservations": "parking",
            "check_in_parking": "parking",
            "admin_cancel_parking": "parking",
            "mark_parking_no_show": "parking",
            "override_parking_no_show": "parking",
            "complete_parking": "parking",
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
            "PL": "EARNED",
            "EL": "EARNED",
            "EARNED": "EARNED",
            "EARNED_LEAVE": "EARNED",
            "PRIVILEGE": "EARNED",
            "PRIVILEGE_LEAVE": "EARNED",
        }
        result = aliases.get(normalized, normalized)
        if result not in {"CASUAL", "SICK", "EARNED"}:
            raise ValueError("unsupported leave type")
        return result
