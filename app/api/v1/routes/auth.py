"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import AuthServiceDep, ClientIp, CurrentUser, UserAgent
from app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    TokenPair,
)
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, auth_service: AuthServiceDep) -> UserRead:
    """Create an account.

    Returns the profile only - the client then calls ``/auth/login``. Keeping
    registration and token issuing separate leaves room for an email
    verification gate without changing this contract.
    """
    user = await auth_service.register(
        email=payload.email,
        full_name=payload.full_name,
        password=payload.password,
        institute=payload.institute,
    )
    return UserRead.model_validate(user)


@router.post("/login", response_model=TokenPair)
async def login(
    payload: LoginRequest,
    auth_service: AuthServiceDep,
    user_agent: UserAgent,
    client_ip: ClientIp,
) -> TokenPair:
    user = await auth_service.authenticate(email=payload.email, password=payload.password)
    return await auth_service.issue_token_pair(
        user, user_agent=user_agent, ip_address=client_ip
    )


@router.post("/refresh", response_model=TokenPair)
async def refresh(
    payload: RefreshRequest,
    auth_service: AuthServiceDep,
    user_agent: UserAgent,
    client_ip: ClientIp,
) -> TokenPair:
    """Exchange a refresh token for a new pair. The old token is revoked."""
    return await auth_service.refresh(
        payload.refresh_token, user_agent=user_agent, ip_address=client_ip
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest,
    auth_service: AuthServiceDep,
    current_user: CurrentUser,
) -> None:
    """Revoke one session, or every session when no token is supplied."""
    if payload.refresh_token:
        await auth_service.revoke(payload.refresh_token)
    else:
        await auth_service.revoke_all_for_user(current_user.id)
