"""Anti-proxy security: OTP codes, bound devices, face enrolments, blocks, alerts.

Phase 4 proves *a phone* was in the room. These tables let the server decide
whether it was *the right person's* phone:

* ``otp_codes``        — email verification, so accounts belong to real people.
* ``user_devices``     — the one phone a student marks attendance from, and the
                         public key every submission must be signed with.
* ``face_enrollments`` — embeddings only; a photo is never stored.
* ``student_blocks``   — a teacher's decision that a student may not mark.
* ``security_alerts``  — what the teachers of a class are told when something
                         looks like proxy attendance.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


def _enum(cls: type[enum.Enum], name: str) -> Enum:
    return Enum(cls, name=name, values_callable=lambda e: [m.value for m in e])


class OtpPurpose(enum.StrEnum):
    EMAIL_VERIFICATION = "email_verification"


class OtpCode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "otp_codes"
    __table_args__ = (
        # Resend limits count a user's recent codes; verification reads the newest.
        Index("ix_otp_codes_user_id_purpose_created_at", "user_id", "purpose", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[OtpPurpose] = mapped_column(_enum(OtpPurpose, "otp_purpose"), nullable=False)

    # HMAC of the code, never the code. A six-digit code has a million values,
    # so a plain hash would be reversed by trying them all; keying it with the
    # server secret means a leaked table alone gives nothing away.
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DevicePlatform(enum.StrEnum):
    ANDROID = "android"
    IOS = "ios"


class UserDevice(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A phone a user has bound. Revoked rows are kept as history."""

    __tablename__ = "user_devices"
    __table_args__ = (
        # One active phone per user. A teacher reset revokes it, which is what
        # lets a new phone bind.
        Index(
            "uq_user_devices_one_active_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        # And one user per active phone, so a single handset cannot mark a
        # whole row of friends from different accounts.
        Index(
            "uq_user_devices_active_public_key",
            "public_key",
            unique=True,
            postgresql_where=text("revoked_at IS NULL"),
        ),
        Index("ix_user_devices_fingerprint_hash", "fingerprint_hash"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Raw 32-byte Ed25519 public key, base64. The private half never leaves
    # the phone's secure storage.
    public_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # SHA-256 of a per-device identifier. Hashed so the server holds no
    # hardware id that could be matched against other services.
    fingerprint_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[DevicePlatform] = mapped_column(
        _enum(DevicePlatform, "device_platform"), nullable=False
    )
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class FaceEnrollment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "face_enrollments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # {"front": [...], "left": [...], "right": [...]} — vectors only. An
    # embedding cannot be turned back into a face; a stored photo would be a
    # liability the moment the database leaked.
    embeddings: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    model_version: Mapped[str] = mapped_column(String(80), nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class StudentBlock(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "student_blocks"
    __table_args__ = (
        Index(
            "uq_student_blocks_one_active",
            "classroom_id",
            "student_id",
            unique=True,
            postgresql_where=text("cleared_at IS NULL"),
        ),
    )

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    blocked_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cleared_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cleared_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AlertType(enum.StrEnum):
    # A second phone tried to bind to an account that already has one.
    MULTI_DEVICE = "multi_device"
    # A phone already bound to someone else tried to bind to this account.
    SHARED_DEVICE = "shared_device"
    # Attendance was submitted from a phone that is not the bound one.
    WRONG_DEVICE = "wrong_device"
    # The bound phone's key did not verify the submission.
    INVALID_SIGNATURE = "invalid_signature"


class AlertSeverity(enum.StrEnum):
    MEDIUM = "medium"
    CRITICAL = "critical"


class SecurityAlert(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per classroom the student belongs to, so each class's teachers
    see and dismiss their own copy."""

    __tablename__ = "security_alerts"
    __table_args__ = (
        Index("ix_security_alerts_classroom_id_created_at", "classroom_id", "created_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[AlertType] = mapped_column(_enum(AlertType, "security_alert_type"), nullable=False)
    severity: Mapped[AlertSeverity] = mapped_column(
        _enum(AlertSeverity, "security_alert_severity"), nullable=False
    )
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(lazy="joined")
