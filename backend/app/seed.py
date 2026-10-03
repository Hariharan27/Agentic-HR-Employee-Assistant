from datetime import date

from sqlalchemy import select

from app.core.security import hash_password
from app.infrastructure.database.models import Employee, Holiday, LeaveBalance, User
from app.infrastructure.database.session import SessionLocal


DEMO_USERS = (
    {
        "employee": {"employee_code": "E1001", "name": "Asha Rao", "email": "asha@example.test",
                     "designation": "Software Engineer", "department": "Engineering", "manager_name": "Karthik Iyer",
                     "location": "Chennai", "employment_type": "Permanent", "joining_date": date(2023, 1, 9)},
        "username": "employee", "password": "employee123", "role": "EMPLOYEE",
    },
    {
        "employee": {"employee_code": "M1001", "name": "Karthik Iyer", "email": "karthik@example.test",
                     "designation": "Engineering Manager", "department": "Engineering", "manager_name": None,
                     "location": "Chennai", "employment_type": "Permanent", "joining_date": date(2020, 6, 1)},
        "username": "manager", "password": "manager123", "role": "MANAGER",
    },
    {
        "employee": {"employee_code": "H1001", "name": "Meera Nair", "email": "meera@example.test",
                     "designation": "HR Partner", "department": "People", "manager_name": None,
                     "location": "Bengaluru", "employment_type": "Permanent", "joining_date": date(2021, 4, 12)},
        "username": "hr", "password": "hr12345", "role": "HR",
    },
)


def seed() -> None:
    with SessionLocal.begin() as db:
        for item in DEMO_USERS:
            user = db.scalar(select(User).where(User.username == item["username"]))
            employee = db.scalar(select(Employee).where(Employee.employee_code == item["employee"]["employee_code"]))
            if employee is None:
                employee = Employee(**item["employee"])
                db.add(employee)
                db.flush()
            if user is None:
                db.add(User(username=item["username"], password_hash=hash_password(item["password"]),
                            role=item["role"], employee_id=employee.id))
            balance_values = {
                "EMPLOYEE": (("CASUAL", 12, 8), ("PRIVILEGE", 18, 3), ("SICK", 10, 1)),
                "MANAGER": (("CASUAL", 12, 2), ("PRIVILEGE", 18, 4), ("SICK", 10, 0)),
                "HR": (("CASUAL", 12, 1), ("PRIVILEGE", 18, 2), ("SICK", 10, 0)),
            }
            for leave_type, total, used in balance_values[item["role"]]:
                exists = db.scalar(select(LeaveBalance.id).where(
                    LeaveBalance.employee_id == employee.id, LeaveBalance.leave_type == leave_type
                ))
                if exists is None:
                    db.add(LeaveBalance(employee_id=employee.id, leave_type=leave_type,
                                        total_days=total, used_days=used))

        # Keep the display name for compatibility, but use a trusted employee FK
        # for approval authorization.
        employee = db.scalar(select(Employee).where(Employee.employee_code == "E1001"))
        manager = db.scalar(select(Employee).where(Employee.employee_code == "M1001"))
        if employee is not None and manager is not None:
            employee.manager_employee_id = manager.id

        year = date.today().year
        for holiday_date, name in (
            (date(year, 1, 26), "Republic Day"),
            (date(year, 8, 15), "Independence Day"),
            (date(year, 10, 2), "Gandhi Jayanti"),
            (date(year, 12, 25), "Christmas"),
        ):
            if db.scalar(select(Holiday.id).where(Holiday.holiday_date == holiday_date)) is None:
                db.add(Holiday(holiday_date=holiday_date, name=name, category="PUBLIC"))


if __name__ == "__main__":
    seed()
