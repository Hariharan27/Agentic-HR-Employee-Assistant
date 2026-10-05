"""Leave rules as stated in the company Leave Policy (v1.3, 02-Mar-2026).

Each rule quotes the policy's own facts and cites its page so answers built from these rules can
show the source. Only what the policy states is enforced in code; this module adds no new limits.
Rules marked `enforced` are checked by LeaveService.build_leave_plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.leave.entities import LeaveType

POLICY_DOCUMENT = "Revised Leave Policy - I2I.pdf"
SICK_BACKDATE_DAYS = 7  # company decision for PeopleDesk; the policy itself requires advance approval


@dataclass(frozen=True, slots=True)
class LeaveRule:
    key: str
    text: str
    page: int
    leave_types: tuple[LeaveType, ...] = ()
    enforced: bool = False
    section: str | None = None

    def applies_to(self, leave_type: LeaveType | None) -> bool:
        return leave_type is None or not self.leave_types or leave_type in self.leave_types

    def to_dict(self) -> dict[str, object]:
        return {
            "rule": self.key,
            "text": self.text,
            "enforced_by_system": self.enforced,
            "source": {"document": POLICY_DOCUMENT, "page": self.page, "section": self.section},
        }


ALL = (LeaveType.CASUAL, LeaveType.SICK, LeaveType.EARNED)

LEAVE_RULES: tuple[LeaveRule, ...] = (
    LeaveRule("entitlement_casual", "Casual Leave: 6 days per calendar year, credited 1.5 days at the start of each quarter.",
              1, (LeaveType.CASUAL,), section="3. Annual Leave Entitlement"),
    LeaveRule("entitlement_sick", "Sick Leave: 6 days per calendar year, credited 1.5 days at the start of each quarter.",
              1, (LeaveType.SICK,), section="3. Annual Leave Entitlement"),
    LeaveRule("entitlement_earned", "Earned Leave: 12 days per calendar year, credited 3 days at the start of each quarter.",
              1, (LeaveType.EARNED,), section="3. Annual Leave Entitlement"),
    LeaveRule("calendar_year", "Leave entitlements follow the calendar year (1 January to 31 December).",
              1, ALL, enforced=True, section="2. Policy Scope"),
    LeaveRule("casual_purpose_lapse", "Casual Leave is for short-term or unforeseen personal needs; it cannot be carried forward or encashed and lapses at year end.",
              2, (LeaveType.CASUAL,), section="4.1 Casual Leave"),
    LeaveRule("sick_purpose_lapse", "Sick Leave applies to illness or medical emergency; it cannot be carried forward or encashed and lapses at year end.",
              2, (LeaveType.SICK,), section="4.2 Sick Leave"),
    LeaveRule("earned_carry_forward", "Up to 8 unused Earned Leave days carry forward; the Earned Leave balance is capped at 20 days and any excess lapses.",
              2, (LeaveType.EARNED,), section="4.3 Earned Leave"),
    LeaveRule("earned_encashment", "Up to 8 Earned Leave days per calendar year can be encashed, once a year, on Basic Pay through payroll; not available during notice period.",
              3, (LeaveType.EARNED,), section="5. Earned Leave Encashment"),
    LeaveRule("any_order", "Leave types can be used in any order, subject to manager approval.",
              2, ALL, section="4.3 Note"),
    LeaveRule("notice_period", "Employees serving their resignation notice period cannot avail CL, SL, PL, EL or WFH.",
              3, ALL, section="5. Note"),
    LeaveRule("advance_approval", "Leave must be applied and approved in advance by the reporting manager; unapproved absence is Leave Without Pay.",
              7, ALL, section="7.1 Leave Application & Approval"),
    LeaveRule("holidays_not_counted", "Public holidays and weekly offs inside an approved leave period are not counted as leave.",
              8, ALL, enforced=True, section="7.3 Holidays and Weekly Offs During Leave"),
    LeaveRule("privilege_leave", "Privilege Leave (PL) is a separate legacy balance retained after the one-time EL conversion and has no expiry.",
              3, (), section="5.1 PL to EL Conversion Logic"),
)


def rules_for(leave_type: LeaveType | None) -> list[LeaveRule]:
    return [rule for rule in LEAVE_RULES if rule.applies_to(leave_type)]
