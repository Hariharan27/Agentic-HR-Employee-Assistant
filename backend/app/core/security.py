from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.exceptions import AuthenticationError

password_hash = PasswordHash.recommended()


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    user_id: int
    employee_id: int
    role: str


def hash_password(password: str) -> str:
    return password_hash.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    return password_hash.verify(password, encoded)


def create_access_token(user: AuthenticatedUser, settings: Settings) -> str:
    now = datetime.now(UTC)
    claims = {
        "sub": str(user.user_id),
        "employee_id": user.employee_id,
        "role": user.role,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_expiry_minutes),
    }
    return jwt.encode(claims, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str, settings: Settings) -> AuthenticatedUser:
    try:
        claims = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        return AuthenticatedUser(
            user_id=int(claims["sub"]),
            employee_id=int(claims["employee_id"]),
            role=str(claims["role"]).upper(),
        )
    except (InvalidTokenError, KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Invalid or expired authentication token") from exc


def require_role(user: AuthenticatedUser, *allowed_roles: str) -> None:
    if user.role.upper() not in {role.upper() for role in allowed_roles}:
        from app.core.exceptions import AuthorizationError
        raise AuthorizationError("You are not authorized to perform this action")
