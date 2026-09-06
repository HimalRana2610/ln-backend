"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError
from app.db.session import get_db
from app.models.user import User
from app.services.auth_service import AuthService

# auto_error=False so a missing header raises our own AuthenticationError,
# keeping the error body shape identical across every failure mode.
_bearer = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_auth_service(db: DbSession) -> AuthService:
    return AuthService(db)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


async def get_current_user(
    auth_service: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Not authenticated")
    return await auth_service.user_from_access_token(credentials.credentials)


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_client_ip(request: Request) -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is only trustworthy behind a proxy that overwrites it;
    the value is recorded for display in a sessions list, never for authorisation.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.client.host if request.client else None


ClientIp = Annotated[str | None, Depends(get_client_ip)]


def get_user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


UserAgent = Annotated[str | None, Depends(get_user_agent)]
