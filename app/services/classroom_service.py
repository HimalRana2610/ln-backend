"""Classroom use cases.

Pure application logic - no FastAPI imports.
"""

from __future__ import annotations

import secrets
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.classroom import (
    CLASS_CODE_LENGTH,
    Classroom,
    ClassroomMember,
    ClassroomType,
    MemberRole,
)
from app.models.user import User
from app.schemas.classroom import THEME_COLORS, ClassroomRead, MemberRead

# Excludes nothing: the old app's codes used the full alphanumeric range and
# users are expected to copy/paste or scan them rather than transcribe by ear.
_CODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
_MAX_CODE_ATTEMPTS = 10


class ClassroomService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- helpers ---------------------------------------------------------

    async def _generate_code(self) -> str:
        """A unique join code.

        Retries on collision rather than trusting randomness: with 36^6 codes a
        clash is rare, but "rare" is not "never", and the unique index would
        otherwise surface it as a 500 at an arbitrary moment.
        """
        for _ in range(_MAX_CODE_ATTEMPTS):
            code = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(CLASS_CODE_LENGTH))
            existing = await self.db.execute(select(Classroom.id).where(Classroom.code == code))
            if existing.scalar_one_or_none() is None:
                return code
        raise ConflictError("Could not allocate a unique class code, please retry")

    async def _membership(self, classroom_id: uuid.UUID, user_id: uuid.UUID) -> ClassroomMember:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            # Deliberately "not found", not "forbidden": a non-member should not
            # be able to discover that a classroom id exists at all.
            raise NotFoundError("Classroom not found")
        return member

    async def _member_count(self, classroom_id: uuid.UUID) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(ClassroomMember)
            .where(ClassroomMember.classroom_id == classroom_id)
        )
        return int(result.scalar_one())

    async def _to_read(self, classroom: Classroom, role: MemberRole) -> ClassroomRead:
        return ClassroomRead(
            id=classroom.id,
            name=classroom.name,
            section=classroom.section,
            code=classroom.code,
            type=classroom.type,
            theme_color=classroom.theme_color,
            owner_id=classroom.owner_id,
            owner_name=classroom.owner.full_name,
            my_role=role,
            member_count=await self._member_count(classroom.id),
            created_at=classroom.created_at,
        )

    # -- commands --------------------------------------------------------

    async def create(
        self,
        *,
        owner: User,
        name: str,
        section: str | None,
        type_: ClassroomType,
        theme_color: str | None,
    ) -> ClassroomRead:
        classroom = Classroom(
            name=name,
            section=section,
            type=type_,
            theme_color=theme_color or THEME_COLORS[0],
            code=await self._generate_code(),
            owner_id=owner.id,
        )
        self.db.add(classroom)
        await self.db.flush()

        # The creator is a member too, so member queries need no special case
        # for the owner.
        self.db.add(
            ClassroomMember(
                classroom_id=classroom.id,
                user_id=owner.id,
                role=MemberRole.OWNER,
            )
        )
        await self.db.flush()
        await self.db.refresh(classroom)

        return await self._to_read(classroom, MemberRole.OWNER)

    async def join_by_code(self, *, user: User, code: str) -> ClassroomRead:
        result = await self.db.execute(select(Classroom).where(Classroom.code == code))
        classroom = result.scalar_one_or_none()
        if classroom is None:
            raise NotFoundError("No classroom has that code")

        existing = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom.id,
                ClassroomMember.user_id == user.id,
            )
        )
        already = existing.scalar_one_or_none()
        if already is not None:
            raise ConflictError("You are already in this classroom")

        self.db.add(
            ClassroomMember(
                classroom_id=classroom.id,
                user_id=user.id,
                role=MemberRole.STUDENT,
            )
        )
        await self.db.flush()

        return await self._to_read(classroom, MemberRole.STUDENT)

    async def update(
        self,
        *,
        classroom_id: uuid.UUID,
        user: User,
        changes: dict[str, object],
    ) -> ClassroomRead:
        membership = await self._membership(classroom_id, user.id)
        if not membership.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can edit this classroom")

        classroom = await self.db.get(Classroom, classroom_id)
        if classroom is None:
            raise NotFoundError("Classroom not found")

        for field, value in changes.items():
            setattr(classroom, field, value)
        await self.db.flush()

        return await self._to_read(classroom, membership.role)

    async def delete(self, *, classroom_id: uuid.UUID, user: User) -> None:
        membership = await self._membership(classroom_id, user.id)
        if membership.role is not MemberRole.OWNER:
            raise PermissionDeniedError("Only the owner can delete this classroom")

        classroom = await self.db.get(Classroom, classroom_id)
        if classroom is None:
            raise NotFoundError("Classroom not found")

        # Memberships cascade at the database level.
        await self.db.delete(classroom)
        await self.db.flush()

    async def leave(self, *, classroom_id: uuid.UUID, user: User) -> None:
        membership = await self._membership(classroom_id, user.id)
        if membership.role is MemberRole.OWNER:
            raise ConflictError(
                "The owner cannot leave. Transfer ownership or delete the classroom."
            )

        await self.db.delete(membership)
        await self.db.flush()

    async def remove_member(
        self, *, classroom_id: uuid.UUID, user: User, target_user_id: uuid.UUID
    ) -> None:
        membership = await self._membership(classroom_id, user.id)
        if not membership.role.can_manage_members:
            raise PermissionDeniedError("Only teachers can remove members")

        target = await self._membership(classroom_id, target_user_id)
        if target.role is MemberRole.OWNER:
            raise ConflictError("The owner cannot be removed")

        await self.db.delete(target)
        await self.db.flush()

    async def set_member_role(
        self,
        *,
        classroom_id: uuid.UUID,
        user: User,
        target_user_id: uuid.UUID,
        role: MemberRole,
    ) -> MemberRead:
        membership = await self._membership(classroom_id, user.id)
        if membership.role is not MemberRole.OWNER:
            raise PermissionDeniedError("Only the owner can change roles")

        target = await self._membership(classroom_id, target_user_id)
        if target.role is MemberRole.OWNER:
            raise ConflictError("The owner's role cannot be changed")

        target.role = role
        await self.db.flush()

        return MemberRead(
            id=target.id,
            user_id=target.user_id,
            email=target.user.email,
            full_name=target.user.full_name,
            role=target.role,
            joined_at=target.created_at,
        )

    # -- queries ---------------------------------------------------------

    async def list_for_user(self, user: User) -> list[ClassroomRead]:
        result = await self.db.execute(
            select(Classroom, ClassroomMember.role)
            .join(ClassroomMember, ClassroomMember.classroom_id == Classroom.id)
            .where(ClassroomMember.user_id == user.id)
            .order_by(Classroom.created_at.desc())
        )
        return [await self._to_read(classroom, role) for classroom, role in result.all()]

    async def get_for_user(self, *, classroom_id: uuid.UUID, user: User) -> ClassroomRead:
        membership = await self._membership(classroom_id, user.id)
        classroom = await self.db.get(Classroom, classroom_id)
        if classroom is None:
            raise NotFoundError("Classroom not found")
        return await self._to_read(classroom, membership.role)

    async def list_members(
        self, *, classroom_id: uuid.UUID, user: User
    ) -> list[MemberRead]:
        # Membership check doubles as the access check.
        await self._membership(classroom_id, user.id)

        result = await self.db.execute(
            select(ClassroomMember)
            .join(User, User.id == ClassroomMember.user_id)
            .where(ClassroomMember.classroom_id == classroom_id)
            .order_by(ClassroomMember.role, User.full_name)
        )
        return [
            MemberRead(
                id=member.id,
                user_id=member.user_id,
                email=member.user.email,
                full_name=member.user.full_name,
                role=member.role,
                joined_at=member.created_at,
            )
            for member in result.scalars().all()
        ]
