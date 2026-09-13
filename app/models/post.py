"""Classroom posts — materials, announcements and assignments — and submissions.

One table for all three kinds of post, because they share almost everything:
an author, a title, a body and optionally a file. They differ by two nullable
columns, which is not enough to justify three tables and three sets of
endpoints.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.note import Asset
    from app.models.user import User


class PostKind(enum.StrEnum):
    MATERIAL = "material"
    ANNOUNCEMENT = "announcement"
    ASSIGNMENT = "assignment"


class ClassroomPost(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "classroom_posts"
    __table_args__ = (
        # The classroom page lists one kind at a time, newest first.
        Index(
            "ix_classroom_posts_classroom_id_kind_created_at",
            "classroom_id",
            "kind",
            "created_at",
        ),
    )

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    kind: Mapped[PostKind] = mapped_column(
        Enum(PostKind, name="post_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Assignments only. Stored with a timezone so every client can render it in
    # the viewer's own zone rather than the teacher's.
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # SET NULL rather than CASCADE: losing the file must not silently delete the
    # post. The service deletes the asset *and* its object together with the post.
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )

    author: Mapped[User] = relationship(lazy="joined")
    asset: Mapped[Asset | None] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ClassroomPost {self.kind.value} {self.title!r}>"


class Submission(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "submissions"
    __table_args__ = (
        # One submission per student per assignment. Resubmitting replaces the
        # row's file; the constraint makes a double-tap unable to duplicate it.
        UniqueConstraint("post_id", "student_id", name="uq_submissions_post_student"),
    )

    post_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classroom_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    student: Mapped[User] = relationship(lazy="joined")
    asset: Mapped[Asset] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Submission {self.student_id} → {self.post_id}>"
