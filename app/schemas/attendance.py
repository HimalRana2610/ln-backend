"""Attendance session, record and verification bodies."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models.attendance import RecordStatus, SessionStatus
from app.services.beacon import MAX_HOP_DEPTH, TOKEN_BYTES

# Six hours of 30-second windows. A phone reports each window at most once per
# hop, so anything longer is not a real lecture.
MAX_OBSERVATIONS = 720 * (MAX_HOP_DEPTH + 1)


class SessionCreate(BaseModel):
    # The classroom's calendar day as the teacher's device sees it.
    date: date
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    radius_meters: int = Field(default=15, ge=5, le=500)
    # How long positions are tracked before students may mark themselves.
    threshold_minutes: int = Field(default=5, ge=1, le=180)
    # Medium signal starts here; Strong is 10 dB above. -80 matches the old app.
    rssi_threshold: int = Field(default=-80, ge=-100, le=-40)
    hop_depth: int = Field(default=MAX_HOP_DEPTH, ge=0, le=MAX_HOP_DEPTH)


class SessionUpdate(BaseModel):
    # `active` opens verification early; `ended` closes the session. There is
    # no way back to `monitoring`, and nothing else about a session can change.
    status: Literal[SessionStatus.ACTIVE, SessionStatus.ENDED]


class SessionRead(BaseModel):
    id: uuid.UUID
    classroom_id: uuid.UUID
    started_by: uuid.UUID
    started_by_name: str
    date: date
    # Effective status: a `monitoring` session whose verification time has
    # passed reads as `active`, with no write needed to flip it.
    status: SessionStatus
    started_at: datetime
    verification_opens_at: datetime
    ended_at: datetime | None
    latitude: float | None
    longitude: float | None
    radius_meters: int
    threshold_minutes: int
    rssi_threshold: int
    hop_depth: int

    # What a phone needs to take part in the beacon.
    session_tag: str
    window_seconds: int
    server_time: datetime
    # Teachers only — null for students. Their phones advertise tokens from it.
    beacon_secret: str | None

    present_count: int
    record_count: int
    # Students only — their own record's status.
    my_status: RecordStatus | None = None


class RecordRead(BaseModel):
    id: uuid.UUID
    session_id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    student_email: str
    status: RecordStatus
    marked_at: datetime | None
    corrected: bool


class RecordUpdate(BaseModel):
    status: Literal[RecordStatus.PRESENT, RecordStatus.ABSENT]


class Observation(BaseModel):
    """One beacon a phone heard: which window, its token, how strong, how far."""

    window: int = Field(ge=0)
    token: str = Field(pattern=rf"^[0-9a-fA-F]{{{TOKEN_BYTES * 2}}}$")
    rssi: int = Field(ge=-127, le=20)
    hop: int = Field(ge=0, le=255)


class VerifyRequest(BaseModel):
    """Beacon evidence, signed by the student's bound phone.

    The security fields are optional in the schema so that a request missing
    them is *recorded* as an unsigned attempt, rather than bounced as a 422
    that leaves no trace in the evidence trail.
    """

    observations: list[Observation] = Field(max_length=MAX_OBSERVATIONS)
    device_id: uuid.UUID | None = None
    nonce: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{16,64}$")
    issued_at: int | None = None
    # Base64 Ed25519 signature over `app.services.signing.message`.
    signature: str | None = Field(default=None, max_length=128)
    # The phone's claim that a fingerprint or face unlock just succeeded. The
    # server cannot check it — it is signed, so it cannot be added in transit,
    # but a rooted phone can lie. Device binding is what makes that costly.
    biometric_verified: bool = False
    device_id_hash: str | None = Field(default=None, max_length=128)


class RejectionReason(enum.StrEnum):
    # Who is asking (Phase 5), checked before any evidence.
    EMAIL_NOT_VERIFIED = "email_not_verified"
    STUDENT_BLOCKED = "student_blocked"
    UNSIGNED = "unsigned"
    WRONG_DEVICE = "wrong_device"
    STALE_REQUEST = "stale_request"
    REPLAYED = "replayed"
    INVALID_SIGNATURE = "invalid_signature"
    BIOMETRIC_REQUIRED = "biometric_required"

    # What the evidence shows (Phase 4).
    SESSION_NOT_OPEN = "session_not_open"
    SESSION_ENDED = "session_ended"
    INVALID_TOKEN = "invalid_token"  # noqa: S105 - a reason code, not a credential
    SIGNAL_TOO_WEAK = "signal_too_weak"
    HOP_LIMIT_EXCEEDED = "hop_limit_exceeded"
    NO_VALID_EVIDENCE = "no_valid_evidence"
    STALE_SIGNAL = "stale_signal"
    INSUFFICIENT_PRESENCE = "insufficient_presence"


class VerifyResult(BaseModel):
    """The outcome of an attempt.

    A rejection is an answer, not an error: it is returned with 200 so the
    attempt's evidence row is committed rather than rolled back with the request.
    """

    accepted: bool
    reason: RejectionReason | None
    message: str
    valid_windows: int
    elapsed_windows: int
    required_windows: int
    record: RecordRead | None


class VerificationRead(BaseModel):
    id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    created_at: datetime
    accepted: bool
    rejection_reason: str | None
    avg_rssi: int | None
    hop_count: int | None
    valid_windows: int
    elapsed_windows: int
