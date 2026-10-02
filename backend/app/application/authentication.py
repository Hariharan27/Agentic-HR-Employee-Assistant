from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, NotFoundError
from app.core.security import AuthenticatedUser, create_access_token, verify_password
from app.infrastructure.database.models import Employee, User


class AuthenticationService:
    def __init__(self, db: Session, settings: Settings):
        self.db = db
        self.settings = settings

    def login(self, username: str, password: str) -> tuple[str, User]:
        user = self.db.scalar(select(User).where(User.username == username))
        if user is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid username or password")
        context = AuthenticatedUser(user_id=user.id, employee_id=user.employee_id, role=user.role)
        return create_access_token(context, self.settings), user

    def profile(self, context: AuthenticatedUser) -> tuple[User, Employee]:
        user = self.db.get(User, context.user_id)
        employee = self.db.get(Employee, context.employee_id)
        if user is None or employee is None or user.employee_id != employee.id:
            raise NotFoundError("Authenticated employee profile was not found")
        return user, employee

