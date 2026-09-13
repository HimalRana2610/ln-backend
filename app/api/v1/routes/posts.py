"""Classroom post, submission and download endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import CurrentUser, DbSession
from app.models.post import PostKind
from app.schemas.post import (
    DownloadLink,
    PostCreate,
    PostRead,
    PostUpdate,
    SubmissionCreate,
    SubmissionRead,
)
from app.services.post_service import PostService

router = APIRouter(tags=["posts"])


def get_service(db: DbSession) -> PostService:
    return PostService(db)


ServiceDep = Annotated[PostService, Depends(get_service)]


@router.get("/classrooms/{classroom_id}/posts", response_model=list[PostRead])
async def list_posts(
    classroom_id: uuid.UUID,
    service: ServiceDep,
    current_user: CurrentUser,
    kind: Annotated[PostKind | None, Query()] = None,
) -> list[PostRead]:
    """A classroom's posts, newest first.

    Assignments carry `submission_count` for teachers and `my_submitted_at` for
    students.
    """
    return await service.list_for_classroom(
        classroom_id=classroom_id, user=current_user, kind=kind
    )


@router.post(
    "/classrooms/{classroom_id}/posts",
    response_model=PostRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_post(
    classroom_id: uuid.UUID,
    payload: PostCreate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> PostRead:
    """Post a material, announcement or assignment. Teachers only.

    To attach a file, first `POST /uploads/presign` with `purpose: attachment`,
    PUT the file to the returned URL, then send its `asset_id` here.
    """
    return await service.create(
        classroom_id=classroom_id,
        user=current_user,
        kind=payload.kind,
        title=payload.title,
        description=payload.description,
        due_date=payload.due_date,
        asset_id=payload.asset_id,
    )


@router.get("/posts/{post_id}", response_model=PostRead)
async def get_post(post_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser) -> PostRead:
    return await service.get(post_id=post_id, user=current_user)


@router.patch("/posts/{post_id}", response_model=PostRead)
async def update_post(
    post_id: uuid.UUID,
    payload: PostUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> PostRead:
    """Edit a post. Author or a teacher only. The file cannot be swapped."""
    return await service.update(
        post_id=post_id,
        user=current_user,
        changes=payload.model_dump(exclude_unset=True),
    )


@router.delete("/posts/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_post(post_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser) -> None:
    """Delete a post, its submissions, and every stored file behind them."""
    await service.delete(post_id=post_id, user=current_user)


@router.post(
    "/posts/{post_id}/submissions",
    response_model=SubmissionRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit(
    post_id: uuid.UUID,
    payload: SubmissionCreate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> SubmissionRead:
    """Hand in work for an assignment. Students only.

    Submitting again replaces the earlier file. Work after the due date is
    accepted and marked `is_late`.
    """
    return await service.submit(post_id=post_id, user=current_user, asset_id=payload.asset_id)


@router.get("/posts/{post_id}/submissions", response_model=list[SubmissionRead])
async def list_submissions(
    post_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> list[SubmissionRead]:
    """Every submission for an assignment. Teachers only."""
    return await service.list_submissions(post_id=post_id, user=current_user)


@router.get("/posts/{post_id}/submissions/me", response_model=SubmissionRead)
async def my_submission(
    post_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> SubmissionRead:
    """Your own submission, or 404 when you have not submitted."""
    return await service.my_submission(post_id=post_id, user=current_user)


@router.get("/assets/{asset_id}/download", response_model=DownloadLink)
async def download_asset(
    asset_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> DownloadLink:
    """A short-lived URL to fetch a file straight from storage.

    Returned rather than redirected to, so clients can show a progress bar and
    choose a filename. Request a fresh one per download; they expire.
    """
    return await service.download_link(asset_id=asset_id, user=current_user)
