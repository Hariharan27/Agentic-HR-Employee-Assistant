from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.domain.onboarding.entities import (
    TASK_TITLES,
    ActivatedAccountData,
    EmployeeReference,
    OnboardingCandidate,
    OnboardingRequestData,
    OnboardingStatus,
    OnboardingTaskData,
    OnboardingTaskStatus,
    OnboardingTaskType,
)
from app.infrastructure.database.models import (
    Employee,
    LeaveBalance,
    OnboardingRequest,
    OnboardingTask,
    User,
)


class SQLAlchemyOnboardingRepository:
    def __init__(self, db: Session):
        self.db = db

    def employee_email_exists(self, email: str) -> bool:
        statement = select(Employee.id).where(func.lower(Employee.email) == email.casefold()).limit(1)
        return self.db.scalar(statement) is not None

    def next_employee_code(self) -> str:
        year = datetime.now(UTC).strftime("%y")
        prefix = f"I{year}"

        employee_codes = self.db.scalars(
            select(Employee.employee_code)
            .where(Employee.employee_code.like(f"{prefix}%"))
        ).all()

        max_sequence = 0

        for code in employee_codes:
            sequence_part = code[len(prefix):]

            if sequence_part.isdigit():
                max_sequence = max(max_sequence, int(sequence_part))

        next_sequence = max_sequence + 1

        return f"{prefix}{next_sequence:03d}"

    def active_onboarding_email_exists(self, email: str) -> bool:
        statement = select(OnboardingRequest.id).where(
            func.lower(OnboardingRequest.email) == email.casefold(),
            OnboardingRequest.status.notin_([
                OnboardingStatus.CANCELLED.value,
                OnboardingStatus.REJECTED.value,
            ]),
        ).limit(1)
        return self.db.scalar(statement) is not None

    def list_reporting_managers(self) -> list[EmployeeReference]:
        rows = self.db.execute(
            select(Employee)
            .join(User, User.employee_id == Employee.id)
            .where(User.role.in_(("MANAGER", "HR")))
            .order_by(Employee.name.asc(), Employee.id.asc())
        ).scalars().all()
        return [
            EmployeeReference(row.id, row.name, row.employee_code, row.designation, row.department)
            for row in rows
        ]

    def find_employee_by_name(self, name: str) -> EmployeeReference | None:
        row = self.db.scalar(
            select(Employee)
            .where(func.lower(Employee.name) == name.casefold())
            .order_by(Employee.id)
            .limit(1)
        )
        return (
            EmployeeReference(row.id, row.name, row.employee_code, row.designation, row.department)
            if row
            else None
        )

    def add_request(
        self,
        candidate: OnboardingCandidate,
        manager_employee_id: int,
        created_by_user_id: int,
    ) -> OnboardingRequestData:
        row = OnboardingRequest(
            employee_name=candidate.name,
            email=candidate.email,
            designation=candidate.designation,
            department=candidate.department,
            manager_employee_id=manager_employee_id,
            manager_name=candidate.reporting_manager,
            joining_date=candidate.joining_date,
            location=candidate.location,
            employment_type=candidate.employment_type,
            status=OnboardingStatus.PENDING_APPROVAL.value,
            created_by_user_id=created_by_user_id,
        )
        self.db.add(row)
        self.db.flush()
        return self._to_request_data(row, ())

    def add_tasks(
        self, request_id: int, task_types: tuple[OnboardingTaskType, ...]
    ) -> tuple[OnboardingTaskData, ...]:
        rows = [
            OnboardingTask(
                onboarding_request_id=request_id,
                task_type=task_type.value,
                title=TASK_TITLES[task_type],
                status=OnboardingTaskStatus.PENDING.value,
            )
            for task_type in task_types
        ]
        self.db.add_all(rows)
        self.db.flush()
        return tuple(self._to_task_data(row) for row in rows)

    def get_request(
        self, request_id: int, *, for_update: bool = False
    ) -> OnboardingRequestData | None:
        statement = select(OnboardingRequest).where(OnboardingRequest.id == request_id)
        if for_update:
            statement = statement.with_for_update()
        row = self.db.scalar(statement)
        if row is None:
            return None
        tasks = self.db.scalars(
            select(OnboardingTask)
            .where(OnboardingTask.onboarding_request_id == request_id)
            .order_by(OnboardingTask.id)
        ).all()
        return self._to_request_data(row, tuple(self._to_task_data(task) for task in tasks))

    def find_requests_by_employee(self, query: str) -> list[OnboardingRequestData]:
        normalized = query.strip().casefold()
        name_prefix = f"{normalized} %"
        rows = self.db.scalars(
            select(OnboardingRequest)
            .where(
                (func.lower(OnboardingRequest.email) == normalized)
                | (func.lower(OnboardingRequest.employee_name) == normalized)
                | (func.lower(OnboardingRequest.employee_name).like(name_prefix))
            )
            .order_by(OnboardingRequest.created_at.desc(), OnboardingRequest.id.desc())
        ).all()
        return [
            self._to_request_data(
                row,
                tuple(
                    self._to_task_data(task)
                    for task in self.db.scalars(
                        select(OnboardingTask)
                        .where(OnboardingTask.onboarding_request_id == row.id)
                        .order_by(OnboardingTask.id)
                    ).all()
                ),
            )
            for row in rows
        ]

    def list_pending_approvals(self) -> list[OnboardingRequestData]:
        rows = self.db.scalars(
            select(OnboardingRequest)
            .where(OnboardingRequest.status == OnboardingStatus.PENDING_APPROVAL.value)
            .order_by(OnboardingRequest.created_at.asc(), OnboardingRequest.id.asc())
        ).all()
        requests = [self.get_request(row.id) for row in rows]
        return [request for request in requests if request is not None]

    def activate_request(
        self,
        request_id: int,
        reviewer_user_id: int,
        comment: str | None,
        employee_code: str,
        username: str,
        password_hash: str,
    ) -> ActivatedAccountData:
        row = self.db.get(OnboardingRequest, request_id)
        if row is None:
            raise NotFoundError("Onboarding request was not found")
        employee = Employee(
            employee_code=employee_code,
            name=row.employee_name,
            email=row.email,
            designation=row.designation,
            department=row.department,
            manager_name=row.manager_name,
            manager_employee_id=row.manager_employee_id,
            location=row.location,
            employment_type=row.employment_type,
            joining_date=row.joining_date,
        )
        self.db.add(employee)
        self.db.flush()
        user = User(
            username=username,
            password_hash=password_hash,
            role="EMPLOYEE",
            employee_id=employee.id,
            must_change_password=True,
        )
        self.db.add(user)
        self.db.flush()
        self.db.add_all([
            LeaveBalance(
                employee_id=employee.id,
                leave_type=leave_type,
                total_days=total_days,
                used_days=0,
                carry_forward_limit_days=carry_forward_limit_days,
            )
            for leave_type, total_days, carry_forward_limit_days in (
                ("CASUAL", 6, 0),
                ("SICK", 6, 0),
                ("EARNED", 12, 8),
            )
        ])
        row.status = OnboardingStatus.ACTIVE.value
        row.reviewed_by_user_id = reviewer_user_id
        row.review_comment = comment
        row.reviewed_at = datetime.now(UTC)
        row.activated_employee_id = employee.id
        row.activated_user_id = user.id
        self.db.flush()
        request = self.get_request(request_id)
        if request is None:
            raise NotFoundError("Onboarding request was not found")
        return ActivatedAccountData(request, employee_code, username, user.id)

    def reject_request(
        self, request_id: int, reviewer_user_id: int, reason: str
    ) -> OnboardingRequestData:
        row = self.db.get(OnboardingRequest, request_id)
        if row is None:
            raise NotFoundError("Onboarding request was not found")
        row.status = OnboardingStatus.REJECTED.value
        row.reviewed_by_user_id = reviewer_user_id
        row.review_comment = reason
        row.reviewed_at = datetime.now(UTC)
        self.db.flush()
        request = self.get_request(request_id)
        if request is None:
            raise NotFoundError("Onboarding request was not found")
        return request

    def update_task_status(
        self, task_id: int, status: OnboardingTaskStatus
    ) -> OnboardingTaskData:
        row = self.db.get(OnboardingTask, task_id)
        if row is None:
            raise NotFoundError("Onboarding task was not found")
        row.status = status.value
        self.db.flush()
        return self._to_task_data(row)

    def update_request_status(
        self, request_id: int, status: OnboardingStatus
    ) -> OnboardingRequestData:
        row = self.db.get(OnboardingRequest, request_id)
        if row is None:
            raise NotFoundError("Onboarding request was not found")
        row.status = status.value
        self.db.flush()
        tasks = self.db.scalars(
            select(OnboardingTask)
            .where(OnboardingTask.onboarding_request_id == request_id)
            .order_by(OnboardingTask.id)
        ).all()
        return self._to_request_data(row, tuple(self._to_task_data(task) for task in tasks))

    @staticmethod
    def _to_task_data(row: OnboardingTask) -> OnboardingTaskData:
        return OnboardingTaskData(
            row.id,
            row.onboarding_request_id,
            OnboardingTaskType(row.task_type),
            row.title,
            OnboardingTaskStatus(row.status),
            row.created_at,
            row.updated_at,
        )

    @staticmethod
    def _to_request_data(
        row: OnboardingRequest, tasks: tuple[OnboardingTaskData, ...]
    ) -> OnboardingRequestData:
        candidate = OnboardingCandidate(
            row.employee_name,
            row.email,
            row.designation,
            row.department,
            row.manager_name,
            row.joining_date,
            row.location,
            row.employment_type,
        )
        return OnboardingRequestData(
            row.id,
            candidate,
            row.manager_employee_id,
            row.created_by_user_id,
            OnboardingStatus(row.status),
            tasks,
            row.created_at,
            row.updated_at,
            row.reviewed_by_user_id,
            row.review_comment,
            row.reviewed_at,
            row.activated_employee_id,
            row.activated_user_id,
        )

    def commit(self) -> None:
        self.db.commit()

    def rollback(self) -> None:
        self.db.rollback()
