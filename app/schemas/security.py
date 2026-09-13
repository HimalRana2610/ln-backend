"""OTP, device, face, block and alert bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.security import AlertSeverity, AlertType, DevicePlatform, UserDevice


class OtpVerifyRequest(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")


class OtpSent(BaseModel):
    sent_to: str
    resend_after_seconds: int


class DeviceRegister(BaseModel):
    public_key: str = Field(min_length=40, max_length=64)
    fingerprint_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    platform: DevicePlatform
    model: str | None = Field(default=None, max_length=120)


class DeviceRead(BaseModel):
    id: uuid.UUID
    platform: DevicePlatform
    model: str | None
    bound_at: datetime
    last_seen_at: datetime

    @classmethod
    def of(cls, device: UserDevice) -> DeviceRead:
        return cls(
            id=device.id,
            platform=device.platform,
            model=device.model,
            bound_at=device.created_at,
            last_seen_at=device.last_seen_at,
        )


class BlockRead(BaseModel):
    classroom_id: uuid.UUID
    classroom_name: str
    reason: str | None
    blocked_at: datetime


class MySecurityStatus(BaseModel):
    email_verified: bool
    device: DeviceRead | None
    face_enrolled: bool
    blocks: list[BlockRead]


class StudentSecurityRead(BaseModel):
    student_id: uuid.UUID
    full_name: str
    email: str
    email_verified: bool
    device: DeviceRead | None
    face_enrolled: bool
    blocked: bool
    block_reason: str | None
    unread_alerts: int


class BlockUpdate(BaseModel):
    blocked: bool
    reason: str | None = Field(default=None, max_length=500)


class AlertRead(BaseModel):
    id: uuid.UUID
    classroom_id: uuid.UUID
    classroom_name: str
    student_id: uuid.UUID
    student_name: str
    student_email: str
    type: AlertType
    severity: AlertSeverity
    message: str
    created_at: datetime
    read_at: datetime | None


class FaceStatus(BaseModel):
    available: bool
    enrolled: bool
    enrolled_at: datetime | None


class FaceMatch(BaseModel):
    matched: bool
    score: float
    threshold: float
