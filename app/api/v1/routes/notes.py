"""Note and upload endpoints."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query, status

from app.api.deps import CurrentUser, DbSession
from app.core.config import settings
from app.db.session import SessionFactory
from app.schemas.note import (
    NoteCreate,
    NoteRead,
    NoteSummary,
    NoteUpdate,
    PresignUploadRequest,
    PresignUploadResponse,
)
from app.services.note_service import NoteService

logger = logging.getLogger("ln.notes")

router = APIRouter(tags=["notes"])


def get_service(db: DbSession) -> NoteService:
    return NoteService(db)


ServiceDep = Annotated[NoteService, Depends(get_service)]


# A background task may start before the request's transaction has committed,
# so the first claim can legitimately find nothing. Retry briefly before giving
# up rather than leaving the note stranded.
_DRAIN_ATTEMPTS = 5
_DRAIN_BACKOFF_SECONDS = 0.4

# Upper bound on notes handled per request, so one task cannot run forever.
_DRAIN_MAX_NOTES = 5


async def _drain_pending() -> None:
    """Generate queued notes after the response has been sent.

    Claims work rather than processing one known id, which matters for a reason
    that is easy to miss: FastAPI can run a background task *before* the
    request's transaction commits, and a fresh session then cannot see the note
    that was just created. Processing by id fails silently in that window and
    leaves the note stuck on `pending` forever.

    Claiming instead makes the task idempotent and shares exactly the code path
    the standalone worker uses, so the two deployment shapes cannot drift.
    """
    processed = 0
    empty_attempts = 0
    logger.info("drain started")

    # Only *empty* claims count against the budget. Finding one note and then
    # stopping at the next empty claim would abandon a note created moments
    # earlier by another request — exactly the burst this runs in.
    while processed < _DRAIN_MAX_NOTES and empty_attempts < _DRAIN_ATTEMPTS:
        async with SessionFactory() as session:
            service = NoteService(session)

            note_id = await service.claim_next_pending()
            # Commit the claim before the slow part: a crash mid-generation then
            # leaves the note visibly `processing`, and the staleness window
            # returns it to the queue rather than losing it.
            await session.commit()

            if note_id is None:
                empty_attempts += 1
                await asyncio.sleep(_DRAIN_BACKOFF_SECONDS)
                continue

            logger.info("generating note %s", note_id)
            await service.process(note_id)
            await session.commit()
            logger.info("finished note %s", note_id)

            processed += 1

    logger.info("drain finished, processed %d", processed)


@router.post(
    "/uploads/presign",
    response_model=PresignUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def presign_upload(
    payload: PresignUploadRequest, service: ServiceDep, current_user: CurrentUser
) -> PresignUploadResponse:
    """Get a URL to PUT a file straight to storage.

    The file never passes through this API. A lecture recording is tens of
    megabytes; proxying it would hold a request open for minutes and exceed a
    serverless function's timeout outright.

    Send the returned `content_type` as the `Content-Type` header on the PUT, or
    the signature will not match.
    """
    asset, url = await service.create_upload_slot(
        user=current_user,
        filename=payload.filename,
        content_type=payload.content_type,
        size_bytes=payload.size_bytes,
    )
    return PresignUploadResponse(
        asset_id=asset.id,
        upload_url=url,
        content_type=payload.content_type,
        expires_in=settings.s3_presign_ttl_seconds,
    )


@router.post(
    "/classrooms/{classroom_id}/notes",
    response_model=NoteRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_note(
    classroom_id: uuid.UUID,
    payload: NoteCreate,
    service: ServiceDep,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
) -> NoteRead:
    """Start generating a note.

    Returns **202** immediately with `status: pending`. Poll `GET /notes/{id}`
    until the status is `ready` or `failed` — generation takes minutes, which is
    far longer than any HTTP request should live.
    """
    note = await service.create(
        classroom_id=classroom_id,
        user=current_user,
        lecture_date=payload.date,
        title=payload.title,
        text=payload.text,
        youtube_url=payload.youtube_url,
        asset_id=payload.asset_id,
    )

    if settings.notes_inline_worker:
        background_tasks.add_task(_drain_pending)

    return note


@router.get("/classrooms/{classroom_id}/notes", response_model=list[NoteSummary])
async def list_notes(
    classroom_id: uuid.UUID,
    service: ServiceDep,
    current_user: CurrentUser,
    on_date: Annotated[date | None, Query(alias="date")] = None,
) -> list[NoteSummary]:
    """Notes in a classroom, newest first. Excludes the Markdown body."""
    return await service.list_for_classroom(
        classroom_id=classroom_id, user=current_user, on_date=on_date
    )


@router.get("/notes/{note_id}", response_model=NoteRead)
async def get_note(
    note_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> NoteRead:
    return await service.get(note_id=note_id, user=current_user)


@router.patch("/notes/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID,
    payload: NoteUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> NoteRead:
    """Edit a note. Author or a teacher only."""
    return await service.update(
        note_id=note_id,
        user=current_user,
        changes=payload.model_dump(exclude_unset=True),
    )


@router.delete("/notes/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> None:
    await service.delete(note_id=note_id, user=current_user)
