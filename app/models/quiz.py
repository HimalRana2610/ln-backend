"""Live classroom quizzes.

A teacher poses one multiple-choice question at a time; students answer once.
The leaderboard is computed from ``quiz_answers`` on read rather than stored,
so it can never disagree with the answers it summarises.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User

OPTION_LETTERS = ("A", "B", "C", "D")


class QuizStatus(enum.StrEnum):
    ACTIVE = "active"
    ENDED = "ended"


class QuizQuestion(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "quiz_questions"
    __table_args__ = (
        Index("ix_quiz_questions_classroom_id_started_at", "classroom_id", "started_at"),
        # One live question per classroom, for the same reason as attendance:
        # two at once would split the room's attention and the leaderboard.
        Index(
            "uq_quiz_questions_one_active",
            "classroom_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
    )

    classroom_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("classrooms.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    prompt: Mapped[str] = mapped_column(String(1000), nullable=False)
    # Exactly four, in A-D order.
    options: Mapped[list[str]] = mapped_column(ARRAY(String(300)), nullable=False)
    correct_option: Mapped[str] = mapped_column(String(1), nullable=False)
    status: Mapped[QuizStatus] = mapped_column(
        Enum(QuizStatus, name="quiz_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class QuizAnswer(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "quiz_answers"
    __table_args__ = (
        # One answer each, and no changing it: the constraint, not the service,
        # is what makes a double-tap or a second client unable to answer twice.
        UniqueConstraint("question_id", "student_id", name="uq_quiz_answers_question_student"),
    )

    question_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("quiz_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    option: Mapped[str] = mapped_column(String(1), nullable=False)
    is_correct: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Whole seconds from the question starting to this answer, measured on the
    # server's clock so a student cannot claim a faster time.
    penalty_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    student: Mapped[User] = relationship(lazy="joined")
