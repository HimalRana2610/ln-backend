"""Current-user endpoints."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.me import AccountDelete
from app.schemas.user import UserRead, UserUpdate
from app.services.me_service import MeService

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


@router.post("/me/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_me(payload: AccountDelete, current_user: CurrentUser, db: DbSession) -> None:
    """Permanently delete your account, every classroom you own, and every file
    behind them. Requires your password. Cannot be undone.

    A POST rather than DELETE because it carries a body, which some proxies
    strip from DELETE requests.
    """
    await MeService(db).delete_account(user=current_user, password=payload.password)
