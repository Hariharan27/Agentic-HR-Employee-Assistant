from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
import re

from app.core.exceptions import AuthenticationError, NotFoundError, PasswordChangeRequiredError, ValidationError
from app.core.security import AuthenticatedUser, create_access_token, hash_password, verify_password
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

    def change_password(self, context: AuthenticatedUser, current_password: str, new_password: str) -> User:
        user = self.db.get(User, context.user_id)
        if user is None or not verify_password(current_password, user.password_hash):
            raise AuthenticationError("The current password is incorrect")
        problems = self.password_problems(new_password, user.username)
        if verify_password(new_password, user.password_hash):
            problems.append("must be different from the current password")
        if problems:
            raise ValidationError("The new password " + "; ".join(problems) + ".")
        user.password_hash = hash_password(new_password)
        user.must_change_password = False
        self.db.commit()
        return user

    @staticmethod
    def password_problems(password: str, username: str) -> list[str]:
        problems = []
        if len(password) < 10:
            problems.append("must be at least 10 characters")
        if not (re.search(r"[a-z]", password) and re.search(r"[A-Z]", password)):
            problems.append("must contain upper- and lower-case letters")
        if not re.search(r"\d", password):
            problems.append("must contain a number")
        if username and username.casefold() in password.casefold():
            problems.append("must not contain the username")
        return problems

    def require_password_changed(self, context: AuthenticatedUser) -> None:
        user = self.db.get(User, context.user_id)
        if user is not None and user.must_change_password:
            raise PasswordChangeRequiredError(
                "Please change your temporary password before using PeopleDesk."
            )

    def profile(self, context: AuthenticatedUser) -> tuple[User, Employee]:
        user = self.db.get(User, context.user_id)
        employee = self.db.get(Employee, context.employee_id)
        if user is None or employee is None or user.employee_id != employee.id:
            raise NotFoundError("Authenticated employee profile was not found")
        return user, employee

