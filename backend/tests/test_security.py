from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import Settings
from app.core.exceptions import AuthenticationError, AuthorizationError
from app.core.security import AuthenticatedUser, create_access_token, decode_access_token, require_role


def settings() -> Settings:
    return Settings(jwt_secret="unit-test-secret-with-enough-length", jwt_expiry_minutes=10)


def test_jwt_round_trip_preserves_trusted_identity():
    original = AuthenticatedUser(user_id=7, employee_id=42, role="MANAGER")
    decoded = decode_access_token(create_access_token(original, settings()), settings())
    assert decoded == original


def test_expired_jwt_is_rejected():
    config = settings()
    token = jwt.encode({"sub": "1", "employee_id": 2, "role": "EMPLOYEE",
                        "exp": datetime.now(UTC) - timedelta(seconds=1)}, config.jwt_secret, algorithm=config.jwt_algorithm)
    with pytest.raises(AuthenticationError):
        decode_access_token(token, config)


def test_role_authorization_is_deterministic():
    manager = AuthenticatedUser(1, 2, "MANAGER")
    require_role(manager, "MANAGER", "HR")
    with pytest.raises(AuthorizationError):
        require_role(AuthenticatedUser(2, 3, "EMPLOYEE"), "MANAGER", "HR")

