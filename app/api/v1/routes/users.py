"""Current-user endpoints."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUser, DbSession
from app.schemas.user import UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserRead)
async def read_me(current_user: CurrentUser) -> UserRead:
    return UserRead.model_validate(current_user)


@router.patch("/me", response_model=UserRead)
async def update_me(
    payload: UserUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> UserRead:
    # exclude_unset so omitting a field leaves it alone, rather than nulling it.
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(current_user, field, value)
    await db.flush()
    return UserRead.model_validate(current_user)
