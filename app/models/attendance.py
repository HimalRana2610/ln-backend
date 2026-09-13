"""Attendance sessions, per-student records, and the evidence behind them.

Proximity is proven over Bluetooth LE. The teacher's phone advertises a token
that rotates every 30 seconds; students' phones record which tokens they heard,
how strongly, and through how many relays. The server alone decides whether that
evidence is enough — see ``attendance_service`` for the rules and
``beacon`` for the token itself.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class SessionStatus(enum.StrEnum):
    # Positions are tracked but nobody may mark yet.
    MONITORING = "monitoring"
    # Students may submit verification.
    ACTIVE = "active"
    ENDED = "ended"


class RecordStatus(enum.StrEnum):
    PENDING = "pending"
    PRESENT = "present"
    ABSENT = "absent"


class AttendanceSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attendance_sessions"
    __table_args__ = (
        # The attendance tab lists a classroom's sessions, newest first.
        Index("ix_attendance_sessions_classroom_id_started_at", "classroom_id", "started_at"),
        # At most one open session per classroom, enforced by the database so
        # two teachers tapping Start together cannot both succeed.
        Index(
            "uq_attendance_sessions_one_open_per_classroom",
            "classroom_id",
            unique=True,
            postgresql_where=text("status <> 'ended'"),
        ),
    )

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    started_by: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # The classroom's local calendar day, sent by the teacher's device. Derived
    # from `started_at` on the server it would be the UTC day, which is the
    # wrong date for a morning lecture in Kathmandu.
    date: Mapped[date] = mapped_column(Date, nullable=False)

    status: Mapped[SessionStatus] = mapped_column(
        Enum(
            SessionStatus,
            name="attendance_session_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    verification_opens_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Recorded for display and for the teacher's own reference. Not enforced:
    # indoors GPS is off by tens of metres and cannot tell floors apart, which
    # is why proximity is proven over Bluetooth instead.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    radius_meters: Mapped[int] = mapped_column(Integer, nullable=False)

    # Fixed when the session starts. Changing either mid-session would make the
    # records within one session incomparable, so there is no way to.
    threshold_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    rssi_threshold: Mapped[int] = mapped_column(Integer, nullable=False)
    hop_depth: Mapped[int] = mapped_column(Integer, nullable=False)

    # Key for the rotating beacon token. Given only to the classroom's teachers,
    # whose phones advertise it; students only ever see tokens derived from it.
    beacon_secret: Mapped[str] = mapped_column(String(64), nullable=False)

    starter: Mapped[User] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceSession {self.classroom_id} {self.date} {self.status.value}>"


class AttendanceRecord(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("session_id", "student_id", name="uq_attendance_records_session_student"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("attendance_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status: Mapped[RecordStatus] = mapped_column(
        Enum(
            RecordStatus,
            name="attendance_record_status",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    marked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set when a teacher overrides the outcome by hand, so an export can tell a
    # correction from a verified mark.
    corrected_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    student: Mapped[User] = relationship(foreign_keys=[student_id], lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AttendanceRecord {self.student_id} {self.status.value}>"


class AttendanceVerification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One row per attempt, accepted or not.

    Rejected attempts are kept on purpose: when a student insists they were in
    the room, this table is the only way to answer.
    """

    __tablename__ = "attendance_verifications"
    __table_args__ = (
        Index("ix_attendance_verifications_session_id_student_id", "session_id", "student_id"),
        # A signed request can be accepted once. Rejected attempts may repeat a
        # nonce — a replay is itself recorded — so the constraint covers only
        # accepted rows; the service checks every row before accepting.
        Index(
            "uq_attendance_verifications_accepted_nonce",
            "device_id",
            "nonce",
            unique=True,
            postgresql_where=text("accepted AND nonce IS NOT NULL"),
        ),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("attendance_sessions.id", ondelete="CASCADE"),
        nullable=False,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    avg_rssi: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hop_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    valid_windows: Mapped[int] = mapped_column(Integer, nullable=False)
    elapsed_windows: Mapped[int] = mapped_column(Integer, nullable=False)

    device_id_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    signature: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The bound device the request claimed to come from, and its one-time
    # nonce. Null for attempts that never got as far as naming a device.
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("user_devices.id", ondelete="SET NULL"), nullable=True
    )
    nonce: Mapped[str | None] = mapped_column(String(64), nullable=True)

    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rejection_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
