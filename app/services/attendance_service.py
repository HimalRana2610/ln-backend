"""Attendance sessions, verification and the rules that decide who was present.

Every rule is enforced here. The phones gather evidence — which beacon windows
they heard, how strongly, through how many relays — and this service decides
whether it is enough. A client that skips its own checks, or lies about them,
gains nothing it could not get by being in the room.

The rules, all from the old app and none relaxed:

* Verification is open only while the session is ``active``.
* Evidence counts only with a genuine token for that window (see ``beacon``),
  a Strong or Medium signal, and no more relays than the session allows.
* The student must have been in range for **70%** of the windows so far.
* The newest evidence must be from this window or the one before, at Strong
  or Medium — being in the room *when marking*, not just earlier.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.attendance import (
    AttendanceRecord,
    AttendanceSession,
    AttendanceVerification,
    RecordStatus,
    SessionStatus,
)
from app.models.classroom import Classroom, ClassroomMember, MemberRole
from app.models.security import AlertSeverity, AlertType, UserDevice
from app.models.user import User
from app.schemas.attendance import (
    Observation,
    RecordRead,
    RejectionReason,
    SessionRead,
    VerificationRead,
    VerifyRequest,
    VerifyResult,
)
from app.services import beacon, signing
from app.services.push_service import PushMessage, PushService
from app.services.security_service import SecurityService


def utcnow() -> datetime:
    """The service's clock. Tests replace it to move through a session."""
    return datetime.now(UTC)


_MESSAGES: dict[RejectionReason, str] = {
    RejectionReason.EMAIL_NOT_VERIFIED: "Verify your email address before marking attendance.",
    RejectionReason.STUDENT_BLOCKED: "Your teacher has blocked you from marking attendance here.",
    RejectionReason.UNSIGNED: "This request was not signed by your phone. Update the app.",
    RejectionReason.WRONG_DEVICE: "Attendance can only be marked from your bound phone.",
    RejectionReason.STALE_REQUEST: "Your phone's clock is wrong, or the request was delayed.",
    RejectionReason.REPLAYED: "This request has already been used.",
    RejectionReason.INVALID_SIGNATURE: "The request's signature did not verify.",
    RejectionReason.BIOMETRIC_REQUIRED: "Confirm with your fingerprint or face to mark attendance.",
    RejectionReason.SESSION_NOT_OPEN: "Verification has not opened yet. Stay in the room.",
    RejectionReason.SESSION_ENDED: "This attendance session has ended.",
    RejectionReason.INVALID_TOKEN: "The beacon evidence did not come from this session.",
    RejectionReason.SIGNAL_TOO_WEAK: "The signal is too weak. Move closer to the teacher.",
    RejectionReason.HOP_LIMIT_EXCEEDED: "The beacon was relayed through too many phones.",
    RejectionReason.NO_VALID_EVIDENCE: "No usable beacon was detected.",
    RejectionReason.STALE_SIGNAL: "The beacon has not been heard in the last minute.",
    RejectionReason.INSUFFICIENT_PRESENCE: "You have not been in range for long enough yet.",
}


@dataclass(frozen=True)
class Evaluation:
    """What a set of observations proves about one student at one moment."""

    reason: RejectionReason | None
    valid_windows: int
    elapsed_windows: int
    required_windows: int
    avg_rssi: int | None
    hop_count: int | None


def effective_status(session: AttendanceSession, now: datetime) -> SessionStatus:
    if session.status is SessionStatus.MONITORING and now >= session.verification_opens_at:
        return SessionStatus.ACTIVE
    return session.status


def evaluate(
    session: AttendanceSession, observations: Sequence[Observation], now: datetime
) -> Evaluation:
    """Apply the attendance rules to a phone's evidence. Pure; no database."""
    current = beacon.window_index(started_at=session.started_at, at=now)
    # Windows begun so far, the current one included.
    elapsed = max(current + 1, 1)
    required = beacon.required_windows(elapsed)

    status = effective_status(session, now)
    if status is not SessionStatus.ACTIVE:
        reason = (
            RejectionReason.SESSION_ENDED
            if status is SessionStatus.ENDED
            else RejectionReason.SESSION_NOT_OPEN
        )
        return Evaluation(reason, 0, elapsed, required, None, None)

    genuine: list[Observation] = []
    for obs in observations:
        # A future window cannot have been heard; its token would be a forgery
        # even if it happened to verify.
        if obs.window > current:
            continue
        if beacon.token_matches(
            secret=session.beacon_secret,
            session_id=session.id,
            window=obs.window,
            presented=obs.token,
        ):
            genuine.append(obs)

    within_hops = [o for o in genuine if o.hop <= session.hop_depth]
    counted = [
        o
        for o in within_hops
        if beacon.signal_counts(rssi=o.rssi, rssi_threshold=session.rssi_threshold)
    ]

    if not counted:
        # Name the first rule the evidence failed, so a student told "no" is
        # also told what to change.
        if not genuine:
            reason = (
                RejectionReason.INVALID_TOKEN if observations else RejectionReason.NO_VALID_EVIDENCE
            )
        elif not within_hops:
            reason = RejectionReason.HOP_LIMIT_EXCEEDED
        else:
            reason = RejectionReason.SIGNAL_TOO_WEAK
        return Evaluation(reason, 0, elapsed, required, None, None)

    valid = len({o.window for o in counted})
    avg_rssi = round(sum(o.rssi for o in counted) / len(counted))
    hop_count = min(o.hop for o in counted)

    def result(reason: RejectionReason | None) -> Evaluation:
        return Evaluation(reason, valid, elapsed, required, avg_rssi, hop_count)

    # "At the moment of marking": the newest genuine reading must be recent,
    # and it must itself be Strong or Medium. A strong signal ten minutes ago
    # followed by a weak one now means the student has walked out.
    newest_window = max(o.window for o in within_hops)
    if newest_window < current - 1:
        return result(RejectionReason.STALE_SIGNAL)
    newest_best = max(o.rssi for o in within_hops if o.window == newest_window)
    if not beacon.signal_counts(rssi=newest_best, rssi_threshold=session.rssi_threshold):
        return result(RejectionReason.SIGNAL_TOO_WEAK)

    if valid < required:
        return result(RejectionReason.INSUFFICIENT_PRESENCE)
    return result(None)


class AttendanceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- access ----------------------------------------------------------

    async def _membership(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> ClassroomMember | None:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def _require_membership(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> ClassroomMember:
        member = await self._membership(classroom_id, user_id)
        if member is None:
            # "Not found", not "forbidden": a non-member must not be able to
            # discover that a classroom exists.
            raise NotFoundError("Classroom not found")
        return member

    async def _session_for_member(
        self, session_id: uuid.UUID, user: User
    ) -> tuple[AttendanceSession, MemberRole]:
        session = await self.db.get(AttendanceSession, session_id)
        member = (
            await self._membership(session.classroom_id, user.id) if session is not None else None
        )
        if session is None or member is None:
            raise NotFoundError("Attendance session not found")
        return session, member.role

    async def _student_ids(self, classroom_id: uuid.UUID) -> list[uuid.UUID]:
        rows = await self.db.scalars(
            select(ClassroomMember.user_id).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.role == MemberRole.STUDENT,
            )
        )
        return list(rows.all())

    # -- mapping ---------------------------------------------------------

    async def _to_reads(
        self, sessions: list[AttendanceSession], *, user: User, role: MemberRole
    ) -> list[SessionRead]:
        now = utcnow()
        ids = [s.id for s in sessions]
        counts: dict[uuid.UUID, tuple[int, int]] = {}
        mine: dict[uuid.UUID, RecordStatus] = {}

        if ids:
            rows = await self.db.execute(
                select(
                    AttendanceRecord.session_id,
                    func.count().filter(AttendanceRecord.status == RecordStatus.PRESENT),
                    func.count(),
                )
                .where(AttendanceRecord.session_id.in_(ids))
                .group_by(AttendanceRecord.session_id)
            )
            counts = {sid: (int(p), int(t)) for sid, p, t in rows.tuples().all()}

            if role is MemberRole.STUDENT:
                own = await self.db.execute(
                    select(AttendanceRecord.session_id, AttendanceRecord.status).where(
                        AttendanceRecord.session_id.in_(ids),
                        AttendanceRecord.student_id == user.id,
                    )
                )
                mine = dict(own.tuples().all())

        reads = []
        for s in sessions:
            present, total = counts.get(s.id, (0, 0))
            reads.append(
                SessionRead(
                    id=s.id,
                    classroom_id=s.classroom_id,
                    started_by=s.started_by,
                    started_by_name=s.starter.full_name,
                    date=s.date,
                    status=effective_status(s, now),
                    started_at=s.started_at,
                    verification_opens_at=s.verification_opens_at,
                    ended_at=s.ended_at,
                    latitude=s.latitude,
                    longitude=s.longitude,
                    radius_meters=s.radius_meters,
                    threshold_minutes=s.threshold_minutes,
                    rssi_threshold=s.rssi_threshold,
                    hop_depth=s.hop_depth,
                    session_tag=beacon.session_tag(s.id),
                    window_seconds=beacon.WINDOW_SECONDS,
                    server_time=now,
                    beacon_secret=s.beacon_secret if role.can_edit_classroom else None,
                    present_count=present,
                    record_count=total,
                    my_status=mine.get(s.id, RecordStatus.PENDING)
                    if role is MemberRole.STUDENT
                    else None,
                )
            )
        return reads

    async def _one_read(
        self, session: AttendanceSession, *, user: User, role: MemberRole
    ) -> SessionRead:
        [read] = await self._to_reads([session], user=user, role=role)
        return read

    @staticmethod
    def _record_to_read(record: AttendanceRecord) -> RecordRead:
        return RecordRead(
            id=record.id,
            session_id=record.session_id,
            student_id=record.student_id,
            student_name=record.student.full_name,
            student_email=record.student.email,
            status=record.status,
            marked_at=record.marked_at,
            corrected=record.corrected_by is not None,
        )

    # -- sessions --------------------------------------------------------

    async def start(
        self,
        *,
        classroom_id: uuid.UUID,
        user: User,
        session_date: date,
        latitude: float | None,
        longitude: float | None,
        radius_meters: int,
        threshold_minutes: int,
        rssi_threshold: int,
        hop_depth: int,
    ) -> SessionRead:
        member = await self._require_membership(classroom_id, user.id)
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can take attendance")

        now = utcnow()
        session = AttendanceSession(
            classroom_id=classroom_id,
            started_by=user.id,
            date=session_date,
            status=SessionStatus.MONITORING,
            started_at=now,
            verification_opens_at=now + timedelta(minutes=threshold_minutes),
            latitude=latitude,
            longitude=longitude,
            radius_meters=radius_meters,
            threshold_minutes=threshold_minutes,
            rssi_threshold=rssi_threshold,
            hop_depth=hop_depth,
            beacon_secret=beacon.new_secret(),
        )

        try:
            # A savepoint, so a lost race surfaces as a clean 409 instead of
            # poisoning the request's transaction.
            async with self.db.begin_nested():
                self.db.add(session)
                await self.db.flush()
        except IntegrityError as exc:
            raise ConflictError("This classroom already has an attendance session open") from exc

        # Every current student starts pending, so the teacher's live list shows
        # who has not arrived rather than only who has.
        self.db.add_all(
            AttendanceRecord(session_id=session.id, student_id=sid, status=RecordStatus.PENDING)
            for sid in await self._student_ids(classroom_id)
        )
        await self.db.flush()
        await self.db.refresh(session)

        await PushService(self.db).send_to_classroom(
            classroom_id,
            PushMessage(
                title="Attendance is open",
                body="Open LectureNote to mark yourself present.",
                data={
                    "type": "attendance",
                    "classroom_id": str(classroom_id),
                    "session_id": str(session.id),
                },
            ),
            roles={MemberRole.STUDENT},
        )
        return await self._one_read(session, user=user, role=member.role)

    async def list_for_classroom(self, *, classroom_id: uuid.UUID, user: User) -> list[SessionRead]:
        member = await self._require_membership(classroom_id, user.id)
        result = await self.db.scalars(
            select(AttendanceSession)
            .where(AttendanceSession.classroom_id == classroom_id)
            .order_by(AttendanceSession.started_at.desc())
        )
        return await self._to_reads(list(result.unique().all()), user=user, role=member.role)

    async def get(self, *, session_id: uuid.UUID, user: User) -> SessionRead:
        session, role = await self._session_for_member(session_id, user)
        return await self._one_read(session, user=user, role=role)

    async def update(
        self, *, session_id: uuid.UUID, user: User, status: SessionStatus
    ) -> SessionRead:
        session, role = await self._session_for_member(session_id, user)
        if not role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can control attendance")

        now = utcnow()
        if session.status is SessionStatus.ENDED:
            raise ConflictError("This attendance session has already ended")

        if status is SessionStatus.ACTIVE:
            # Opening early moves the time forward; it never pushes it back.
            session.status = SessionStatus.ACTIVE
            session.verification_opens_at = min(session.verification_opens_at, now)
        else:
            await self._end(session, now)

        await self.db.flush()
        await self.db.refresh(session)
        return await self._one_read(session, user=user, role=role)

    async def _end(self, session: AttendanceSession, now: datetime) -> None:
        session.status = SessionStatus.ENDED
        session.ended_at = now

        records = await self.db.scalars(
            select(AttendanceRecord).where(AttendanceRecord.session_id == session.id)
        )
        have = set()
        for record in records.unique().all():
            have.add(record.student_id)
            if record.status is RecordStatus.PENDING:
                record.status = RecordStatus.ABSENT

        # Students who joined the classroom mid-session have no row yet. Without
        # one they would silently be missing from the register, not absent.
        self.db.add_all(
            AttendanceRecord(session_id=session.id, student_id=sid, status=RecordStatus.ABSENT)
            for sid in await self._student_ids(session.classroom_id)
            if sid not in have
        )

    # -- verification ----------------------------------------------------

    async def _check_identity(
        self,
        *,
        session: AttendanceSession,
        user: User,
        request: VerifyRequest,
        now: datetime,
    ) -> tuple[RejectionReason | None, UserDevice | None]:
        """Phase 5: is this the right person, on their own phone, asking once?

        Checked before the beacon evidence, in the order a student can fix
        them. Returns the first failure and the device the request named, if it
        is a real bound device.
        """
        security = SecurityService(self.db)

        if not user.is_email_verified:
            return RejectionReason.EMAIL_NOT_VERIFIED, None
        if await security.active_block(classroom_id=session.classroom_id, student_id=user.id):
            return RejectionReason.STUDENT_BLOCKED, None

        if (
            request.device_id is None
            or request.signature is None
            or request.nonce is None
            or request.issued_at is None
        ):
            return RejectionReason.UNSIGNED, None

        bound = await security.active_device(user.id)
        if bound is None or bound.id != request.device_id:
            await security.raise_alert(
                user=user,
                type=AlertType.WRONG_DEVICE,
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"{user.full_name} submitted attendance from a phone that is not the one "
                    "bound to their account."
                    if bound is not None
                    else f"{user.full_name} submitted attendance without a bound phone."
                ),
            )
            return RejectionReason.WRONG_DEVICE, None

        if abs(now.timestamp() - request.issued_at) > settings.signature_max_skew_seconds:
            return RejectionReason.STALE_REQUEST, bound

        seen = await self.db.scalar(
            select(func.count()).where(
                AttendanceVerification.device_id == bound.id,
                AttendanceVerification.nonce == request.nonce,
            )
        )
        if seen:
            return RejectionReason.REPLAYED, bound

        signed = signing.message(
            session_id=session.id,
            user_id=user.id,
            device_id=bound.id,
            nonce=request.nonce,
            issued_at=request.issued_at,
            biometric=request.biometric_verified,
            observations=request.observations,
        )
        if not signing.verify(
            public_key_b64=bound.public_key, signature_b64=request.signature, signed=signed
        ):
            await security.raise_alert(
                user=user,
                type=AlertType.INVALID_SIGNATURE,
                severity=AlertSeverity.CRITICAL,
                message=f"Attendance for {user.full_name} failed signature verification.",
            )
            return RejectionReason.INVALID_SIGNATURE, bound

        if not request.biometric_verified:
            return RejectionReason.BIOMETRIC_REQUIRED, bound

        bound.last_seen_at = now
        return None, bound

    async def verify(
        self, *, session_id: uuid.UUID, user: User, request: VerifyRequest
    ) -> VerifyResult:
        session, role = await self._session_for_member(session_id, user)
        if role is not MemberRole.STUDENT:
            raise PermissionDeniedError("Only students mark attendance")

        record = await self.db.scalar(
            select(AttendanceRecord).where(
                AttendanceRecord.session_id == session.id,
                AttendanceRecord.student_id == user.id,
            )
        )
        now = utcnow()
        outcome = evaluate(session, request.observations, now)
        device_id_hash, signature = request.device_id_hash, request.signature

        if record is not None and record.status is RecordStatus.PRESENT:
            # A double tap, or a retry after a lost response. Already done; no
            # second evidence row for what is the same attempt.
            return VerifyResult(
                accepted=True,
                reason=None,
                message="You are already marked present.",
                valid_windows=outcome.valid_windows,
                elapsed_windows=outcome.elapsed_windows,
                required_windows=outcome.required_windows,
                record=self._record_to_read(record),
            )

        identity_failure, device = await self._check_identity(
            session=session, user=user, request=request, now=now
        )
        if identity_failure is not None:
            # The evidence is still evaluated and stored with the attempt: a
            # teacher reviewing it wants to know whether the phone was in range.
            outcome = replace(outcome, reason=identity_failure)

        self.db.add(
            AttendanceVerification(
                device_id=device.id if device else None,
                nonce=request.nonce if device else None,
                session_id=session.id,
                student_id=user.id,
                avg_rssi=outcome.avg_rssi,
                hop_count=outcome.hop_count,
                valid_windows=outcome.valid_windows,
                elapsed_windows=outcome.elapsed_windows,
                device_id_hash=device_id_hash,
                signature=signature,
                accepted=outcome.reason is None,
                rejection_reason=outcome.reason.value if outcome.reason else None,
            )
        )

        if outcome.reason is None:
            if record is None:
                # Joined the classroom after the session started.
                record = AttendanceRecord(
                    session_id=session.id, student_id=user.id, status=RecordStatus.PRESENT
                )
                self.db.add(record)
            record.status = RecordStatus.PRESENT
            record.marked_at = now

        await self.db.flush()
        if record is not None:
            await self.db.refresh(record)

        return VerifyResult(
            accepted=outcome.reason is None,
            reason=outcome.reason,
            message=_MESSAGES[outcome.reason] if outcome.reason else "You are marked present.",
            valid_windows=outcome.valid_windows,
            elapsed_windows=outcome.elapsed_windows,
            required_windows=outcome.required_windows,
            record=self._record_to_read(record) if record is not None else None,
        )

    # -- records ---------------------------------------------------------

    async def list_records(self, *, session_id: uuid.UUID, user: User) -> list[RecordRead]:
        session, role = await self._session_for_member(session_id, user)
        query = (
            select(AttendanceRecord)
            .join(User, User.id == AttendanceRecord.student_id)
            .where(AttendanceRecord.session_id == session.id)
            .order_by(User.full_name)
        )
        if not role.can_edit_classroom:
            query = query.where(AttendanceRecord.student_id == user.id)
        result = await self.db.scalars(query)
        return [self._record_to_read(r) for r in result.unique().all()]

    async def correct_record(
        self, *, record_id: uuid.UUID, user: User, status: RecordStatus
    ) -> RecordRead:
        record = await self.db.get(AttendanceRecord, record_id)
        session = (
            await self.db.get(AttendanceSession, record.session_id) if record is not None else None
        )
        member = (
            await self._membership(session.classroom_id, user.id) if session is not None else None
        )
        if record is None or member is None:
            raise NotFoundError("Attendance record not found")
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can correct attendance")

        record.status = status
        record.marked_at = utcnow() if status is RecordStatus.PRESENT else None
        record.corrected_by = user.id
        await self.db.flush()
        await self.db.refresh(record)
        return self._record_to_read(record)

    async def list_verifications(
        self, *, session_id: uuid.UUID, user: User
    ) -> list[VerificationRead]:
        session, role = await self._session_for_member(session_id, user)
        if not role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can see verification attempts")

        rows = await self.db.execute(
            select(AttendanceVerification, User.full_name)
            .join(User, User.id == AttendanceVerification.student_id)
            .where(AttendanceVerification.session_id == session.id)
            .order_by(AttendanceVerification.created_at.desc())
        )
        return [
            VerificationRead(
                id=v.id,
                student_id=v.student_id,
                student_name=name,
                created_at=v.created_at,
                accepted=v.accepted,
                rejection_reason=v.rejection_reason,
                avg_rssi=v.avg_rssi,
                hop_count=v.hop_count,
                valid_windows=v.valid_windows,
                elapsed_windows=v.elapsed_windows,
            )
            for v, name in rows.tuples().all()
        ]

    # -- export ----------------------------------------------------------

    async def export(self, *, classroom_id: uuid.UUID, user: User) -> tuple[str, bytes]:
        """The attendance register as a spreadsheet, in the old app's columns.

        Returns ``(filename, xlsx bytes)``. Only ended sessions count — an open
        one would show every student as absent for a lecture still in progress.
        """
        member = await self._require_membership(classroom_id, user.id)
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can export attendance")

        classroom = await self.db.get(Classroom, classroom_id)
        assert classroom is not None  # membership implies it exists

        total = int(
            await self.db.scalar(
                select(func.count()).where(
                    AttendanceSession.classroom_id == classroom_id,
                    AttendanceSession.status == SessionStatus.ENDED,
                )
            )
            or 0
        )
        present_rows = await self.db.execute(
            select(AttendanceRecord.student_id, func.count())
            .join(AttendanceSession, AttendanceSession.id == AttendanceRecord.session_id)
            .where(
                AttendanceSession.classroom_id == classroom_id,
                AttendanceSession.status == SessionStatus.ENDED,
                AttendanceRecord.status == RecordStatus.PRESENT,
            )
            .group_by(AttendanceRecord.student_id)
        )
        present = {sid: int(n) for sid, n in present_rows.tuples().all()}

        students = await self.db.execute(
            select(User)
            .join(ClassroomMember, ClassroomMember.user_id == User.id)
            .where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.role == MemberRole.STUDENT,
            )
            .order_by(User.full_name)
        )

        rows: list[list[object]] = []
        for student in students.scalars().all():
            p = present.get(student.id, 0)
            percentage = f"{(p / total * 100):.1f}%" if total else "0.0%"
            rows.append([student.full_name, student.email, p, total - p, total, percentage])

        today = utcnow().date().isoformat()
        return f"{classroom.name}_Attendance_{today}.xlsx", build_xlsx(rows)


EXPORT_HEADERS = ["Student Name", "Email", "Present", "Absent", "Total Sessions", "Attendance %"]
_EXPORT_WIDTHS = [20, 30, 10, 10, 15, 15]


def build_xlsx(rows: list[list[object]]) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Attendance"
    sheet.append(EXPORT_HEADERS)
    for row in rows:
        sheet.append([_safe_cell(v) for v in row])
    for index, width in enumerate(_EXPORT_WIDTHS):
        sheet.column_dimensions[chr(ord("A") + index)].width = width

    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _safe_cell(value: object) -> object:
    """Neutralise spreadsheet formula injection in user-controlled text.

    A student named ``=HYPERLINK(...)`` would otherwise become a live formula
    in the teacher's copy of Excel.
    """
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value
