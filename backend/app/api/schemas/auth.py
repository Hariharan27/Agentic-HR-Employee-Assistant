from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    must_change_password: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class ProfileResponse(BaseModel):
    user_id: int
    employee_id: int
    employee_code: str
    name: str
    email: str
    role: str
    must_change_password: bool = False

