from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.application.leave.service import LeaveService
from app.core.exceptions import ConflictError
from app.core.security import AuthenticatedUser
from app.domain.leave.dates import resolve_leave_dates
from app.infrastructure.database.models import LeaveRequest, User
from app.infrastructure.repositories.leave import SQLAlchemyLeaveRepository

MONDAY = date(2026, 10, 5)  # the conftest holiday is Wednesday 2026-10-07


def service(db_session):
    return LeaveService(
        SQLAlchemyLeaveRepository(db_session),
        today=lambda: MONDAY,
        now=lambda: datetime(2026, 10, 5, 4, 30, tzinfo=UTC),
    )


def employee(db_session):
    user = db_session.scalar(select(User).where(User.username == "employee"))
    return AuthenticatedUser(user.id, user.employee_id, user.role)


def dates(text):
    return list(resolve_leave_dates(text, MONDAY).dates)


def test_tuesday_and_sunday_plan_counts_one_day_and_names_the_weekly_off(db_session):
    plan = service(db_session).build_leave_plan(employee(db_session), "CASUAL", dates("Tuesday and Sunday"))

    assert plan.eligible
    assert plan.total_working_days == Decimal("1")
    assert [(item.start_date, item.end_date) for item in plan.segments] == [(date(2026, 10, 6), date(2026, 10, 6))]
    assert [(item.day, item.reason) for item in plan.excluded_days] == [(date(2026, 10, 11), "weekly off")]
    assert plan.available_after == Decimal("3")
    assert "2026-10-11 (Sun, weekly off)" in plan.summary()


def test_range_excludes_holiday_and_weekend_but_stays_one_request(db_session):
    plan = service(db_session).build_leave_plan(employee(db_session), "CASUAL", dates("Tuesday to Sunday"))

    assert plan.total_working_days == Decimal("3")
    assert len(plan.segments) == 1
    assert {item.reason for item in plan.excluded_days} == {"holiday", "weekly off"}


def test_separate_days_with_a_working_gap_become_separate_segments(db_session):
    plan = service(db_session).build_leave_plan(
        employee(db_session), "SICK", [date(2026, 10, 6), date(2026, 10, 9)]
    )

    assert [(item.start_date, item.end_date) for item in plan.segments] == [
        (date(2026, 10, 6), date(2026, 10, 6)),
        (date(2026, 10, 9), date(2026, 10, 9)),
    ]
    assert plan.total_working_days == Decimal("2")


def test_past_dates_are_blocked_for_casual_and_allowed_for_recent_sick_leave(db_session):
    casual = service(db_session).build_leave_plan(employee(db_session), "CASUAL", [date(2026, 10, 1)])
    recent_sick = service(db_session).build_leave_plan(employee(db_session), "SICK", [date(2026, 9, 30)])
    old_sick = service(db_session).build_leave_plan(employee(db_session), "SICK", [date(2026, 9, 25)])

    assert not casual.eligible and "past dates" in casual.problems[0]
    assert recent_sick.eligible
    assert not old_sick.eligible and "cannot start before 2026-09-28" in old_sick.problems[0]


def test_insufficient_balance_is_reported_not_raised(db_session):
    plan = service(db_session).build_leave_plan(employee(db_session), "CASUAL", dates("12 Oct to 16 Oct"))

    assert not plan.eligible
    assert "needs 5 day(s), 4 available" in plan.problems[0]


def test_weekend_only_plan_has_no_working_days(db_session):
    plan = service(db_session).build_leave_plan(employee(db_session), "CASUAL", [date(2026, 10, 10), date(2026, 10, 11)])

    assert not plan.eligible
    assert "no working days" in plan.problems[0]


def test_staging_a_plan_creates_one_request_per_segment(db_session):
    leave = service(db_session)
    actor = employee(db_session)
    plan = leave.build_leave_plan(actor, "SICK", [date(2026, 10, 6), date(2026, 10, 9)])

    created = leave.stage_leave_plan(actor, "SICK", plan.requested_dates, plan.fingerprint)
    db_session.commit()

    assert [item.working_days for item in created] == [Decimal("1"), Decimal("1")]
    assert len(db_session.scalars(select(LeaveRequest)).all()) == 2


def test_staging_refuses_a_plan_whose_facts_changed(db_session):
    leave = service(db_session)
    actor = employee(db_session)
    plan = leave.build_leave_plan(actor, "CASUAL", [date(2026, 10, 6)])
    leave.apply_leave(actor, "CASUAL", date(2026, 10, 12), date(2026, 10, 12))  # balance changes

    with pytest.raises(ConflictError, match="changed since this confirmation"):
        leave.stage_leave_plan(actor, "CASUAL", plan.requested_dates, plan.fingerprint)


def test_dates_in_a_later_calendar_year_wait_for_that_year_credit(db_session):
    plan = service(db_session).build_leave_plan(employee(db_session), "CASUAL", [date(2027, 1, 4)])

    assert not plan.eligible
    assert "calendar year" in plan.problems[0] and "2027" in plan.problems[0]


@pytest.mark.parametrize("value", ["PL", "privilege", "Privilege Leave"])
def test_privilege_leave_is_not_treated_as_earned_leave(db_session, value):
    from app.core.exceptions import ValidationError

    with pytest.raises(ValidationError, match="Privilege Leave \\(PL\\) is a separate legacy balance"):
        service(db_session).build_leave_plan(employee(db_session), value, [date(2026, 10, 6)])


def test_holidays_follow_the_employee_location_calendar(db_session):
    from app.infrastructure.database.models import Holiday

    db_session.add_all([
        Holiday(holiday_date=date(2026, 11, 10), name="Diwali", category="PUBLIC", region="KARNATAKA"),
        Holiday(holiday_date=date(2026, 10, 19), name="Ayudha Poojai", category="PUBLIC", region="TAMIL_NADU"),
    ])
    db_session.commit()
    hr_user = db_session.scalar(select(User).where(User.username == "hr"))  # located in Bengaluru
    from app.infrastructure.database.models import LeaveBalance
    db_session.add(LeaveBalance(employee_id=hr_user.employee_id, leave_type="CASUAL", total_days=6, used_days=0))
    db_session.commit()
    hr = AuthenticatedUser(hr_user.id, hr_user.employee_id, hr_user.role)
    leave = service(db_session)

    chennai = leave.build_leave_plan(employee(db_session), "CASUAL", [date(2026, 10, 19), date(2026, 11, 10)])
    bengaluru = leave.build_leave_plan(hr, "CASUAL", [date(2026, 10, 19), date(2026, 11, 10)])

    assert [(item.day, item.name) for item in chennai.excluded_days] == [(date(2026, 10, 19), "Ayudha Poojai")]
    assert [(item.day, item.name) for item in bengaluru.excluded_days] == [(date(2026, 11, 10), "Diwali")]
    assert "holiday: Ayudha Poojai" in chennai.summary()
