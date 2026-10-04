from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.dependencies import get_db
from app.core.config import get_settings
from app.core.security import hash_password
from app.infrastructure.database.base import Base
from app.infrastructure.database.models import Employee, Holiday, LeaveBalance, User
from app.main import app


@pytest.fixture()
def db_session():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)
    with TestingSession() as db:
        employee = Employee(employee_code="E1", name="Test Employee", email="employee@test.local",
                            designation="Engineer", department="Engineering", manager_name="Manager",
                            location="Chennai", employment_type="Permanent", joining_date=date(2024, 1, 1))
        manager = Employee(employee_code="M1", name="Test Manager", email="manager@test.local",
                           designation="Manager", department="Engineering", manager_name=None,
                           location="Chennai", employment_type="Permanent", joining_date=date(2022, 1, 1))
        hr = Employee(employee_code="H1", name="Test HR", email="hr@test.local",
                      designation="HR Partner", department="People", manager_name=None,
                      location="Bengaluru", employment_type="Permanent", joining_date=date(2021, 1, 1))
        hr_admin = Employee(employee_code="HA1", name="Test HR Admin", email="hradmin@test.local",
                            designation="HR Administrator", department="People", manager_name=None,
                            location="Chennai", employment_type="Permanent", joining_date=date(2020, 1, 1))
        parking_admin = Employee(employee_code="PA1", name="Test Parking Admin", email="parking@test.local",
                                 designation="Parking Administrator", department="Workplace Operations",
                                 manager_name=None, location="Chennai", employment_type="Permanent",
                                 joining_date=date(2020, 6, 1))
        db.add_all([employee, manager, hr, hr_admin, parking_admin]); db.flush()
        employee.manager_employee_id = manager.id
        db.add_all([
            User(username="employee", password_hash=hash_password("correct-password"), role="EMPLOYEE", employee_id=employee.id),
            User(username="manager", password_hash=hash_password("manager-password"), role="MANAGER", employee_id=manager.id),
            User(username="hr", password_hash=hash_password("hr-password"), role="HR", employee_id=hr.id),
            User(username="hradmin", password_hash=hash_password("hradmin-password"), role="HR_ADMIN", employee_id=hr_admin.id),
            User(username="parkingadmin", password_hash=hash_password("parkingadmin-password"), role="PARKING_ADMIN", employee_id=parking_admin.id),
        ])
        db.add_all([
            LeaveBalance(employee_id=employee.id, leave_type="CASUAL", total_days=12, used_days=8),
            LeaveBalance(employee_id=employee.id, leave_type="PRIVILEGE", total_days=18, used_days=3),
            LeaveBalance(employee_id=manager.id, leave_type="CASUAL", total_days=12, used_days=2),
            Holiday(holiday_date=date(2026, 10, 7), name="Test Holiday", category="PUBLIC"),
        ])
        db.commit()
        yield db


@pytest.fixture()
def client(db_session):
    def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
