"""Classrooms and membership.

Role lives on the *membership*, not on the user. In the old project a global
``teacher`` / ``student`` flag fought reality: the same person owns one class and
attends another. Here, being a teacher is something you are *in a classroom*.
"""

from __future__ import annotations

import enum
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User

CLASS_CODE_LENGTH = 6


class ClassroomType(enum.StrEnum):
    """``personal`` is shown as "Private" in the UI; the wire value matches the
    old Firestore documents so historic data maps across cleanly."""

    PERSONAL = "personal"
    PUBLIC = "public"


class MemberRole(enum.StrEnum):
    OWNER = "owner"
    TEACHER = "teacher"
    STUDENT = "student"

    @property
    def can_manage_members(self) -> bool:
        return self in {MemberRole.OWNER, MemberRole.TEACHER}

    @property
    def can_edit_classroom(self) -> bool:
        return self in {MemberRole.OWNER, MemberRole.TEACHER}


class Classroom(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classrooms"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    section: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # Six-character join code. Unique so a code always names one classroom.
    code: Mapped[str] = mapped_column(
        String(CLASS_CODE_LENGTH), unique=True, index=True, nullable=False
    )

    type: Mapped[ClassroomType] = mapped_column(
        Enum(ClassroomType, name="classroom_type", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=ClassroomType.PERSONAL.value,
    )

    # Tailwind gradient pair, e.g. "from-blue-500 to-indigo-600". Carried over
    # from the old `themeColor` field so card colours survive the migration.
    theme_color: Mapped[str] = mapped_column(
        String(60), nullable=False, server_default=text("'from-blue-500 to-indigo-600'")
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    owner: Mapped[User] = relationship(foreign_keys=[owner_id], lazy="joined")
    members: Mapped[list[ClassroomMember]] = relationship(
        back_populates="classroom",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Classroom {self.code} {self.name!r}>"


class ClassroomMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classroom_members"
    __table_args__ = (
        # One membership per person per classroom. Enforced by the database, so
        # a double-tap on "join" cannot create a duplicate.
        UniqueConstraint("classroom_id", "user_id", name="uq_classroom_members_classroom_user"),
    )

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole, name="member_role", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=MemberRole.STUDENT.value,
    )

    classroom: Mapped[Classroom] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClassroomMember {self.user_id} {self.role.value}>"
