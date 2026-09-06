"""Request/response bodies for the auth endpoints."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

PASSWORD_MIN_LENGTH = 10


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=128)
    institute: str | None = Field(default=None, max_length=200)

    @field_validator("password")
    @classmethod
    def _reject_trivial_passwords(cls, value: str) -> str:
        """Cheap sanity checks only.

        Deliberately not a complexity matrix - length plus a breached-password
        check (added later) protects far better than forcing a symbol.
        """
        if value.strip() != value:
            raise ValueError("Password must not start or end with whitespace")
        if len(set(value)) < 4:
            raise ValueError("Password is too repetitive")
        return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=1)


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="Token to revoke. Omit to revoke every session for the user.",
    )


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"  # noqa: S105 - scheme name, not a secret
    expires_in: int = Field(description="Access token lifetime in seconds")
