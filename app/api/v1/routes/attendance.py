"""Attendance session, verification, record and export endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Response, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.attendance import (
    RecordRead,
    RecordUpdate,
    SessionCreate,
    SessionRead,
    SessionUpdate,
    VerificationRead,
    VerifyRequest,
    VerifyResult,
)
from app.services.attendance_service import AttendanceService

router = APIRouter(tags=["attendance"])

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def get_service(db: DbSession) -> AttendanceService:
    return AttendanceService(db)


ServiceDep = Annotated[AttendanceService, Depends(get_service)]


@router.post(
    "/classrooms/{classroom_id}/attendance/sessions",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_session(
    classroom_id: uuid.UUID,
    payload: SessionCreate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> SessionRead:
    """Start taking attendance. Teachers only; one open session per classroom.

    The session begins in `monitoring` and reads as `active` once
    `threshold_minutes` have passed. The response carries `beacon_secret`, which
    the teacher's phone uses to advertise the rotating token.
    """
    return await service.start(
        classroom_id=classroom_id,
        user=current_user,
        session_date=payload.date,
        latitude=payload.latitude,
        longitude=payload.longitude,
        radius_meters=payload.radius_meters,
        threshold_minutes=payload.threshold_minutes,
        rssi_threshold=payload.rssi_threshold,
        hop_depth=payload.hop_depth,
    )


@router.get("/classrooms/{classroom_id}/attendance/sessions", response_model=list[SessionRead])
async def list_sessions(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> list[SessionRead]:
    return await service.list_for_classroom(classroom_id=classroom_id, user=current_user)


@router.get("/attendance/sessions/{session_id}", response_model=SessionRead)
async def get_session(
    session_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> SessionRead:
    return await service.get(session_id=session_id, user=current_user)


@router.patch("/attendance/sessions/{session_id}", response_model=SessionRead)
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> SessionRead:
    """`active` opens verification now; `ended` closes the session. Teachers only.

    Ending marks every student still pending as absent.
    """
    return await service.update(session_id=session_id, user=current_user, status=payload.status)


@router.post("/attendance/sessions/{session_id}/verify", response_model=VerifyResult)
async def verify(
    session_id: uuid.UUID,
    payload: VerifyRequest,
    service: ServiceDep,
    current_user: CurrentUser,
) -> VerifyResult:
    """Submit beacon evidence to be marked present. Students only.

    The request must come from the student's bound phone (`POST /devices/register`),
    carry a one-time `nonce` and current `issued_at`, confirm a biometric check,
    and be signed — see `app.services.signing` for the exact message.

    Always 200 for a well-formed attempt: `accepted` says whether it worked and
    `reason` why not. Every attempt is recorded, including rejected ones.
    """
    return await service.verify(session_id=session_id, user=current_user, request=payload)


@router.get("/attendance/sessions/{session_id}/records", response_model=list[RecordRead])
async def list_records(
    session_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> list[RecordRead]:
    """Every record for teachers; a student sees only their own."""
    return await service.list_records(session_id=session_id, user=current_user)


@router.get(
    "/attendance/sessions/{session_id}/verifications", response_model=list[VerificationRead]
)
async def list_verifications(
    session_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> list[VerificationRead]:
    """Every attempt, accepted or rejected, newest first. Teachers only."""
    return await service.list_verifications(session_id=session_id, user=current_user)


@router.patch("/attendance/records/{record_id}", response_model=RecordRead)
async def correct_record(
    record_id: uuid.UUID,
    payload: RecordUpdate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> RecordRead:
    """Mark a student present or absent by hand. Teachers only."""
    return await service.correct_record(
        record_id=record_id, user=current_user, status=payload.status
    )


@router.get(
    "/classrooms/{classroom_id}/attendance/export",
    response_class=Response,
    responses={200: {"content": {XLSX: {}}}},
)
async def export_attendance(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> Response:
    """The register as an .xlsx spreadsheet, in the old app's format. Teachers only."""
    filename, body = await service.export(classroom_id=classroom_id, user=current_user)
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "")
    return Response(
        content=body,
        media_type=XLSX,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"
            )
        },
    )
