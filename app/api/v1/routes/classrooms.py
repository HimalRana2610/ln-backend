"""Classroom endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.classroom import (
    THEME_COLORS,
    ClassroomCreate,
    ClassroomRead,
    ClassroomUpdate,
    JoinClassroomRequest,
    MemberRead,
    MemberRoleUpdate,
)
from app.services.classroom_service import ClassroomService

router = APIRouter(prefix="/classrooms", tags=["classrooms"])


def get_service(db: DbSession) -> ClassroomService:
    return ClassroomService(db)


ServiceDep = Annotated[ClassroomService, Depends(get_service)]


@router.get("/theme-colors", response_model=list[str])
async def list_theme_colors() -> list[str]:
    """Gradient options for class cards.

    Served from the backend so web and mobile cannot drift apart on which
    colours exist.
    """
    return THEME_COLORS


@router.get("", response_model=list[ClassroomRead])
async def list_my_classrooms(
    service: ServiceDep, current_user: CurrentUser
) -> list[ClassroomRead]:
    return await service.list_for_user(current_user)


@router.post("", response_model=ClassroomRead, status_code=status.HTTP_201_CREATED)
async def create_classroom(
    payload: ClassroomCreate, service: ServiceDep, current_user: CurrentUser
) -> ClassroomRead:
    return await service.create(
        owner=current_user,
        name=payload.name,
        section=payload.section,
        type_=payload.type,
        theme_color=payload.theme_color,
    )


@router.post("/join", response_model=ClassroomRead)
async def join_classroom(
    payload: JoinClassroomRequest, service: ServiceDep, current_user: CurrentUser
) -> ClassroomRead:
    return await service.join_by_code(user=current_user, code=payload.code)


@router.get("/{classroom_id}", response_model=ClassroomRead)
async def get_classroom(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> ClassroomRead:
    return await service.get_for_user(classroom_id=classroom_id, user=current_user)


@router.patch("/{classroom_id}", response_model=ClassroomRead)
async def update_classroom(
    classroom_id: uuid.UUID,
    payload: ClassroomUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> ClassroomRead:
    # exclude_unset so omitting a field leaves it alone rather than nulling it.
    return await service.update(
        classroom_id=classroom_id,
        user=current_user,
        changes=payload.model_dump(exclude_unset=True),
    )


@router.delete("/{classroom_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_classroom(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> None:
    await service.delete(classroom_id=classroom_id, user=current_user)


@router.post("/{classroom_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_classroom(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> None:
    await service.leave(classroom_id=classroom_id, user=current_user)


@router.get("/{classroom_id}/members", response_model=list[MemberRead])
async def list_members(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> list[MemberRead]:
    return await service.list_members(classroom_id=classroom_id, user=current_user)


@router.patch("/{classroom_id}/members/{user_id}", response_model=MemberRead)
async def set_member_role(
    classroom_id: uuid.UUID,
    user_id: uuid.UUID,
    payload: MemberRoleUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> MemberRead:
    return await service.set_member_role(
        classroom_id=classroom_id,
        user=current_user,
        target_user_id=user_id,
        role=payload.role,
    )


@router.delete(
    "/{classroom_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_member(
    classroom_id: uuid.UUID,
    user_id: uuid.UUID,
    service: ServiceDep,
    current_user: CurrentUser,
) -> None:
    await service.remove_member(
        classroom_id=classroom_id, user=current_user, target_user_id=user_id
    )
