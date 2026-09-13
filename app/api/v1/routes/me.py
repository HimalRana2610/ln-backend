"""The signed-in user's own cross-classroom resources."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.me import PushTokenRegister, PushTokenRemove, ToDoItem
from app.services.me_service import MeService
from app.services.push_service import PushService

router = APIRouter(prefix="/me", tags=["me"])


def _me(db: DbSession) -> MeService:
    return MeService(db)


def _push(db: DbSession) -> PushService:
    return PushService(db)


MeDep = Annotated[MeService, Depends(_me)]
PushDep = Annotated[PushService, Depends(_push)]


@router.get("/todo", response_model=list[ToDoItem])
async def todo(service: MeDep, current_user: CurrentUser) -> list[ToDoItem]:
    """Every assignment in every class you study in, soonest due first.

    `status` is decided on the server's clock: `done` when submitted, `missing`
    when past due without a submission, otherwise `assigned`.
    """
    return await service.todo(current_user)


@router.post("/devices/push-token", status_code=status.HTTP_204_NO_CONTENT)
async def register_push_token(
    payload: PushTokenRegister, service: PushDep, current_user: CurrentUser
) -> None:
    """Register this device's FCM token. Safe to call on every launch."""
    await service.register(user=current_user, token=payload.token, platform=payload.platform)


@router.post("/devices/push-token/remove", status_code=status.HTTP_204_NO_CONTENT)
async def remove_push_token(
    payload: PushTokenRemove, service: PushDep, current_user: CurrentUser
) -> None:
    """Stop pushes to this device — call on sign-out, before the tokens are cleared."""
    await service.unregister(user=current_user, token=payload.token)
