from fastapi import APIRouter

from app.api.dependencies import AppSettings, CurrentUser, Database
from app.api.schemas.auth import LoginRequest, ProfileResponse, TokenResponse
from app.application.authentication import AuthenticationService

router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Database, settings: AppSettings) -> TokenResponse:
    token, user = AuthenticationService(db, settings).login(body.username, body.password)
    return TokenResponse(access_token=token, role=user.role)


@router.get("/me", response_model=ProfileResponse)
def me(context: CurrentUser, db: Database, settings: AppSettings) -> ProfileResponse:
    user, employee = AuthenticationService(db, settings).profile(context)
    return ProfileResponse(
        user_id=user.id,
        employee_id=employee.id,
        employee_code=employee.employee_code,
        name=employee.name,
        email=employee.email,
        role=user.role,
    )

