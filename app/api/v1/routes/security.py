"""Email verification, device binding, face enrolment and teacher security tools."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile, status

from app.api.deps import CurrentUser, DbSession
from app.core.exceptions import ValidationFailedError
from app.schemas.security import (
    AlertRead,
    BlockUpdate,
    DeviceRead,
    DeviceRegister,
    FaceMatch,
    FaceStatus,
    MySecurityStatus,
    OtpSent,
    OtpVerifyRequest,
    StudentSecurityRead,
)
from app.schemas.user import UserRead
from app.services.face_service import MAX_IMAGE_BYTES, FaceService
from app.services.otp_service import OtpService
from app.services.security_service import SecurityService

router = APIRouter(tags=["security"])


def _otp(db: DbSession) -> OtpService:
    return OtpService(db)


def _security(db: DbSession) -> SecurityService:
    return SecurityService(db)


def _face(db: DbSession) -> FaceService:
    return FaceService(db)


OtpDep = Annotated[OtpService, Depends(_otp)]
SecurityDep = Annotated[SecurityService, Depends(_security)]
FaceDep = Annotated[FaceService, Depends(_face)]


# -- email verification ------------------------------------------------------


async def _send_code(service: OtpService, user: CurrentUser) -> OtpSent:
    wait = await service.send_email_verification(user)
    return OtpSent(sent_to=user.email, resend_after_seconds=wait)


@router.post("/auth/otp/send", response_model=OtpSent)
async def send_otp(service: OtpDep, current_user: CurrentUser) -> OtpSent:
    """Email a six-digit code to the signed-in user. Rate limited (429)."""
    return await _send_code(service, current_user)


@router.post("/auth/otp/resend", response_model=OtpSent)
async def resend_otp(service: OtpDep, current_user: CurrentUser) -> OtpSent:
    """Same as send; retires the previous code. Rate limited (429)."""
    return await _send_code(service, current_user)


@router.post("/auth/otp/verify", response_model=UserRead)
async def verify_otp(
    payload: OtpVerifyRequest, service: OtpDep, current_user: CurrentUser
) -> UserRead:
    """Confirm the code. Errors: `otp_invalid`, `otp_expired`, `otp_locked`."""
    await service.verify_email(current_user, payload.code)
    return UserRead.model_validate(current_user)


# -- devices -------------------------------------------------------------------


@router.post("/devices/register", response_model=DeviceRead)
async def register_device(
    payload: DeviceRegister, service: SecurityDep, current_user: CurrentUser
) -> DeviceRead:
    """Bind this phone's public key to the account, or check in again.

    409 when the account already has a different phone, or this phone belongs
    to another account — both alert the student's teachers.
    """
    return await service.register_device(
        user=current_user,
        public_key=payload.public_key,
        fingerprint_hash=payload.fingerprint_hash,
        platform=payload.platform,
        model=payload.model,
    )


@router.get("/users/me/security", response_model=MySecurityStatus)
async def my_security(service: SecurityDep, current_user: CurrentUser) -> MySecurityStatus:
    return await service.my_status(current_user)


# -- teacher tools -------------------------------------------------------------


@router.get(
    "/classrooms/{classroom_id}/students/security", response_model=list[StudentSecurityRead]
)
async def list_student_security(
    classroom_id: uuid.UUID, service: SecurityDep, current_user: CurrentUser
) -> list[StudentSecurityRead]:
    return await service.list_students(classroom_id=classroom_id, user=current_user)


@router.post(
    "/classrooms/{classroom_id}/students/{student_id}/block",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def block_student(
    classroom_id: uuid.UUID,
    student_id: uuid.UUID,
    payload: BlockUpdate,
    service: SecurityDep,
    current_user: CurrentUser,
) -> None:
    """Block (`blocked: true`) or unblock a student from marking attendance here."""
    await service.set_block(
        classroom_id=classroom_id,
        student_id=student_id,
        user=current_user,
        blocked=payload.blocked,
        reason=payload.reason,
    )


@router.post(
    "/classrooms/{classroom_id}/students/{student_id}/reset-enrollment",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def reset_enrollment(
    classroom_id: uuid.UUID,
    student_id: uuid.UUID,
    service: SecurityDep,
    current_user: CurrentUser,
) -> None:
    """Unbind the student's phone and delete their face enrolment, so they can enrol again."""
    await service.reset_enrollment(
        classroom_id=classroom_id, student_id=student_id, user=current_user
    )


@router.get("/security/alerts", response_model=list[AlertRead])
async def list_alerts(
    service: SecurityDep,
    current_user: CurrentUser,
    classroom_id: Annotated[uuid.UUID | None, Query()] = None,
    unread: Annotated[bool, Query()] = False,
) -> list[AlertRead]:
    """Alerts for classrooms you teach, newest first."""
    return await service.list_alerts(
        user=current_user, classroom_id=classroom_id, unread_only=unread
    )


@router.post("/security/alerts/{alert_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def read_alert(alert_id: uuid.UUID, service: SecurityDep, current_user: CurrentUser) -> None:
    await service.mark_alert_read(alert_id=alert_id, user=current_user)


# -- face ----------------------------------------------------------------------


async def _read_image(upload: UploadFile) -> tuple[bytes, str]:
    content_type = upload.content_type or ""
    if content_type not in {"image/jpeg", "image/png"}:
        raise ValidationFailedError("Photos must be JPEG or PNG", code="invalid_image")
    # One byte over the limit is enough to know; never read an unbounded body.
    data = await upload.read(MAX_IMAGE_BYTES + 1)
    if len(data) > MAX_IMAGE_BYTES:
        raise ValidationFailedError("Photos must be under 5 MB", code="image_too_large")
    return data, content_type


@router.get("/face/status", response_model=FaceStatus)
async def face_status(service: FaceDep, current_user: CurrentUser) -> FaceStatus:
    return await service.status(current_user)


@router.post("/face/enrollment", response_model=FaceStatus)
async def enroll_face(
    service: FaceDep,
    current_user: CurrentUser,
    front: Annotated[UploadFile, File()],
    left: Annotated[UploadFile, File()],
    right: Annotated[UploadFile, File()],
    consent: Annotated[bool, Form()] = False,
) -> FaceStatus:
    """Enrol from three poses. Photos are turned into embeddings and discarded.

    503 when no face recognition service is configured.
    """
    images = {
        "front": await _read_image(front),
        "left": await _read_image(left),
        "right": await _read_image(right),
    }
    return await service.enroll(user=current_user, images=images, consent=consent)


@router.post("/face/verify", response_model=FaceMatch)
async def verify_face(
    service: FaceDep, current_user: CurrentUser, image: Annotated[UploadFile, File()]
) -> FaceMatch:
    data, content_type = await _read_image(image)
    return await service.verify(user=current_user, image=data, content_type=content_type)


@router.delete("/face/enrollment", status_code=status.HTTP_204_NO_CONTENT)
async def delete_face(service: FaceDep, current_user: CurrentUser) -> None:
    """Delete your face embeddings. Always allowed."""
    await service.delete(current_user)
