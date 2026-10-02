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
        db.add_all([employee, manager]); db.flush()
        db.add_all([
            User(username="employee", password_hash=hash_password("correct-password"), role="EMPLOYEE", employee_id=employee.id),
            User(username="manager", password_hash=hash_password("manager-password"), role="MANAGER", employee_id=manager.id),
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
