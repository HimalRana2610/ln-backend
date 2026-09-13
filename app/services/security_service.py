"""Device binding, blocks, alerts and teacher security tools.

Nothing here blocks a student automatically. A second phone or a bad signature
raises an *alert*; deciding that it means cheating is left to a teacher. A
student wrongly locked out before an exam is a worse failure than a proxy
attempt that a teacher reviews an hour later — and every block has an obvious
path to the person who can clear it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.models.classroom import Classroom, ClassroomMember, MemberRole
from app.models.security import (
    AlertSeverity,
    AlertType,
    DevicePlatform,
    FaceEnrollment,
    SecurityAlert,
    StudentBlock,
    UserDevice,
)
from app.models.user import User
from app.schemas.security import (
    AlertRead,
    BlockRead,
    DeviceRead,
    MySecurityStatus,
    StudentSecurityRead,
)
from app.services import signing
from app.services.push_service import PushMessage, PushService


def utcnow() -> datetime:
    return datetime.now(UTC)


class SecurityService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- lookups ---------------------------------------------------------

    async def active_device(self, user_id: uuid.UUID) -> UserDevice | None:
        result = await self.db.execute(
            select(UserDevice).where(UserDevice.user_id == user_id, UserDevice.revoked_at.is_(None))
        )
        return result.scalar_one_or_none()

    async def active_block(
        self, *, classroom_id: uuid.UUID, student_id: uuid.UUID
    ) -> StudentBlock | None:
        result = await self.db.execute(
            select(StudentBlock).where(
                StudentBlock.classroom_id == classroom_id,
                StudentBlock.student_id == student_id,
                StudentBlock.cleared_at.is_(None),
            )
        )
        return result.scalar_one_or_none()

    async def _manager(self, classroom_id: uuid.UUID, user: User) -> ClassroomMember:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id, ClassroomMember.user_id == user.id
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise NotFoundError("Classroom not found")
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can manage student security")
        return member

    async def _student_member(
        self, classroom_id: uuid.UUID, student_id: uuid.UUID
    ) -> ClassroomMember:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.user_id == student_id,
                ClassroomMember.role == MemberRole.STUDENT,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise NotFoundError("Student not found in this classroom")
        return member

    # -- alerts ----------------------------------------------------------

    async def raise_alert(
        self, *, user: User, type: AlertType, severity: AlertSeverity, message: str
    ) -> None:
        """Tell the teachers of every class this person studies in."""
        classroom_ids = await self.db.scalars(
            select(ClassroomMember.classroom_id).where(
                ClassroomMember.user_id == user.id, ClassroomMember.role == MemberRole.STUDENT
            )
        )
        ids = list(classroom_ids.all())
        self.db.add_all(
            SecurityAlert(
                user_id=user.id, classroom_id=cid, type=type, severity=severity, message=message
            )
            for cid in ids
        )
        await self.db.flush()

        push = PushService(self.db)
        for cid in ids:
            await push.send_to_classroom(
                cid,
                PushMessage(
                    title="Security alert",
                    body=f"{user.full_name}: {message}"[:180],
                    data={"type": "security_alert", "classroom_id": str(cid)},
                ),
                roles={MemberRole.OWNER, MemberRole.TEACHER},
            )

    async def list_alerts(
        self, *, user: User, classroom_id: uuid.UUID | None, unread_only: bool
    ) -> list[AlertRead]:
        managed = select(ClassroomMember.classroom_id).where(
            ClassroomMember.user_id == user.id,
            ClassroomMember.role.in_([MemberRole.OWNER, MemberRole.TEACHER]),
        )
        query = (
            select(SecurityAlert, Classroom.name)
            .join(Classroom, Classroom.id == SecurityAlert.classroom_id)
            .where(SecurityAlert.classroom_id.in_(managed))
            .order_by(SecurityAlert.created_at.desc())
            .limit(200)
        )
        if classroom_id is not None:
            await self._manager(classroom_id, user)
            query = query.where(SecurityAlert.classroom_id == classroom_id)
        if unread_only:
            query = query.where(SecurityAlert.read_at.is_(None))

        rows = await self.db.execute(query)
        return [
            AlertRead(
                id=a.id,
                classroom_id=a.classroom_id,
                classroom_name=name,
                student_id=a.user_id,
                student_name=a.user.full_name,
                student_email=a.user.email,
                type=a.type,
                severity=a.severity,
                message=a.message,
                created_at=a.created_at,
                read_at=a.read_at,
            )
            for a, name in rows.unique().tuples().all()
        ]

    async def mark_alert_read(self, *, alert_id: uuid.UUID, user: User) -> None:
        alert = await self.db.get(SecurityAlert, alert_id)
        if alert is None:
            raise NotFoundError("Alert not found")
        try:
            await self._manager(alert.classroom_id, user)
        except (NotFoundError, PermissionDeniedError) as exc:
            raise NotFoundError("Alert not found") from exc
        alert.read_at = alert.read_at or utcnow()
        await self.db.flush()

    # -- device binding --------------------------------------------------

    async def register_device(
        self,
        *,
        user: User,
        public_key: str,
        fingerprint_hash: str,
        platform: DevicePlatform,
        model: str | None,
    ) -> DeviceRead:
        if signing.decode_public_key(public_key) is None:
            raise ValidationFailedError(
                "That is not an Ed25519 public key", code="invalid_public_key"
            )

        now = utcnow()
        current = await self.active_device(user.id)

        if current is not None:
            if current.public_key == public_key:
                # The same phone checking in again.
                current.last_seen_at = now
                current.model = model or current.model
                await self.db.flush()
                return DeviceRead.of(current)

            await self.raise_alert(
                user=user,
                type=AlertType.MULTI_DEVICE,
                severity=AlertSeverity.MEDIUM,
                message=(
                    f"{user.full_name} tried to use a second phone ({model or platform.value}). "
                    "Their account stays bound to the first one."
                ),
            )
            # Committed before refusing: the alert is the point of this path,
            # and the request dependency would otherwise roll it back.
            await self.db.commit()
            raise ConflictError(
                "Your account is already bound to another phone. Ask a teacher to reset it."
            )

        other = await self.db.scalar(
            select(UserDevice).where(
                UserDevice.revoked_at.is_(None),
                UserDevice.user_id != user.id,
                or_(
                    UserDevice.public_key == public_key,
                    UserDevice.fingerprint_hash == fingerprint_hash,
                ),
            )
        )
        if other is not None:
            await self.raise_alert(
                user=user,
                type=AlertType.SHARED_DEVICE,
                severity=AlertSeverity.CRITICAL,
                message=(
                    f"{user.full_name} tried to bind a phone that is already bound to another "
                    "student's account."
                ),
            )
            await self.db.commit()
            raise ConflictError(
                "This phone is already bound to another student's account. "
                "Each student needs their own phone."
            )

        device = UserDevice(
            user_id=user.id,
            public_key=public_key,
            fingerprint_hash=fingerprint_hash,
            platform=platform,
            model=model,
            last_seen_at=now,
        )
        self.db.add(device)
        await self.db.flush()
        return DeviceRead.of(device)

    # -- status ----------------------------------------------------------

    async def my_status(self, user: User) -> MySecurityStatus:
        device = await self.active_device(user.id)
        face = await self.db.scalar(
            select(FaceEnrollment.id).where(FaceEnrollment.user_id == user.id)
        )
        rows = await self.db.execute(
            select(StudentBlock, Classroom.name)
            .join(Classroom, Classroom.id == StudentBlock.classroom_id)
            .where(StudentBlock.student_id == user.id, StudentBlock.cleared_at.is_(None))
        )
        return MySecurityStatus(
            email_verified=user.is_email_verified,
            device=DeviceRead.of(device) if device else None,
            face_enrolled=face is not None,
            blocks=[
                BlockRead(
                    classroom_id=b.classroom_id,
                    classroom_name=name,
                    reason=b.reason,
                    blocked_at=b.created_at,
                )
                for b, name in rows.tuples().all()
            ],
        )

    # -- teacher tools ---------------------------------------------------

    async def list_students(
        self, *, classroom_id: uuid.UUID, user: User
    ) -> list[StudentSecurityRead]:
        await self._manager(classroom_id, user)

        students = (
            (
                await self.db.execute(
                    select(User)
                    .join(ClassroomMember, ClassroomMember.user_id == User.id)
                    .where(
                        ClassroomMember.classroom_id == classroom_id,
                        ClassroomMember.role == MemberRole.STUDENT,
                    )
                    .order_by(User.full_name)
                )
            )
            .scalars()
            .all()
        )
        ids = [s.id for s in students]
        if not ids:
            return []

        devices = {
            d.user_id: d
            for d in (
                await self.db.scalars(
                    select(UserDevice).where(
                        UserDevice.user_id.in_(ids), UserDevice.revoked_at.is_(None)
                    )
                )
            ).all()
        }
        faces = set(
            (
                await self.db.scalars(
                    select(FaceEnrollment.user_id).where(FaceEnrollment.user_id.in_(ids))
                )
            ).all()
        )
        blocks = {
            b.student_id: b
            for b in (
                await self.db.scalars(
                    select(StudentBlock).where(
                        StudentBlock.classroom_id == classroom_id,
                        StudentBlock.student_id.in_(ids),
                        StudentBlock.cleared_at.is_(None),
                    )
                )
            ).all()
        }
        unread = dict(
            (
                await self.db.execute(
                    select(SecurityAlert.user_id, func.count())
                    .where(
                        SecurityAlert.classroom_id == classroom_id,
                        SecurityAlert.user_id.in_(ids),
                        SecurityAlert.read_at.is_(None),
                    )
                    .group_by(SecurityAlert.user_id)
                )
            )
            .tuples()
            .all()
        )

        return [
            StudentSecurityRead(
                student_id=s.id,
                full_name=s.full_name,
                email=s.email,
                email_verified=s.is_email_verified,
                device=DeviceRead.of(devices[s.id]) if s.id in devices else None,
                face_enrolled=s.id in faces,
                blocked=s.id in blocks,
                block_reason=blocks[s.id].reason if s.id in blocks else None,
                unread_alerts=int(unread.get(s.id, 0)),
            )
            for s in students
        ]

    async def set_block(
        self,
        *,
        classroom_id: uuid.UUID,
        student_id: uuid.UUID,
        user: User,
        blocked: bool,
        reason: str | None,
    ) -> None:
        await self._manager(classroom_id, user)
        await self._student_member(classroom_id, student_id)

        existing = await self.active_block(classroom_id=classroom_id, student_id=student_id)
        if blocked and existing is None:
            self.db.add(
                StudentBlock(
                    classroom_id=classroom_id,
                    student_id=student_id,
                    blocked_by=user.id,
                    reason=reason,
                )
            )
        elif blocked and existing is not None:
            existing.reason = reason or existing.reason
        elif existing is not None:
            existing.cleared_at = utcnow()
            existing.cleared_by = user.id
        await self.db.flush()

    async def reset_enrollment(
        self, *, classroom_id: uuid.UUID, student_id: uuid.UUID, user: User
    ) -> None:
        """Unbind the student's phone and delete their face enrolment.

        Applies across every class — a phone is bound to a person, not to a
        classroom — which is why any teacher of any of their classes may do it.
        """
        await self._manager(classroom_id, user)
        await self._student_member(classroom_id, student_id)

        await self.db.execute(
            update(UserDevice)
            .where(UserDevice.user_id == student_id, UserDevice.revoked_at.is_(None))
            .values(revoked_at=utcnow(), revoked_by=user.id)
        )
        face = await self.db.scalar(
            select(FaceEnrollment).where(FaceEnrollment.user_id == student_id)
        )
        if face is not None:
            await self.db.delete(face)
        await self.db.flush()
