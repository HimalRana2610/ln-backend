"""Live quizzes: pose a question, collect one answer each, rank the room.

Clients poll ``GET /classrooms/{id}/quiz`` every ``quiz_poll_interval_seconds``
while the quiz screen is open. The old app had Firestore listeners; a
serverless deployment has no long-lived connection to push over, so polling is
the deliberate trade.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.classroom import ClassroomMember, MemberRole
from app.models.quiz import OPTION_LETTERS, QuizAnswer, QuizQuestion, QuizStatus
from app.models.user import User
from app.schemas.quiz import AnswerResult, LeaderboardEntry, QuestionRead, QuizState
from app.services.push_service import PushMessage, PushService

RECENT_QUESTIONS = 10


def utcnow() -> datetime:
    return datetime.now(UTC)


class QuizService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _membership(self, classroom_id: uuid.UUID, user: User) -> ClassroomMember:
        member = await self.db.scalar(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id, ClassroomMember.user_id == user.id
            )
        )
        if member is None:
            raise NotFoundError("Classroom not found")
        return member

    async def _question_for_member(
        self, question_id: uuid.UUID, user: User
    ) -> tuple[QuizQuestion, ClassroomMember]:
        question = await self.db.get(QuizQuestion, question_id)
        if question is None:
            raise NotFoundError("Question not found")
        try:
            member = await self._membership(question.classroom_id, user)
        except NotFoundError as exc:
            raise NotFoundError("Question not found") from exc
        return question, member

    async def _to_reads(
        self, questions: list[QuizQuestion], *, user: User, role: MemberRole
    ) -> list[QuestionRead]:
        ids = [q.id for q in questions]
        counts: dict[uuid.UUID, int] = {}
        mine: dict[uuid.UUID, tuple[str, bool]] = {}
        if ids:
            rows = await self.db.execute(
                select(QuizAnswer.question_id, func.count())
                .where(QuizAnswer.question_id.in_(ids))
                .group_by(QuizAnswer.question_id)
            )
            counts = {qid: int(n) for qid, n in rows.tuples().all()}
            own = await self.db.execute(
                select(QuizAnswer.question_id, QuizAnswer.option, QuizAnswer.is_correct).where(
                    QuizAnswer.question_id.in_(ids), QuizAnswer.student_id == user.id
                )
            )
            mine = {qid: (option, correct) for qid, option, correct in own.tuples().all()}

        reads = []
        for q in questions:
            reveal = role.can_edit_classroom or q.status is QuizStatus.ENDED
            answer = mine.get(q.id)
            reads.append(
                QuestionRead(
                    id=q.id,
                    classroom_id=q.classroom_id,
                    prompt=q.prompt,
                    options=list(q.options),
                    status=q.status,
                    started_at=q.started_at,
                    ended_at=q.ended_at,
                    correct_option=q.correct_option if reveal else None,  # type: ignore[arg-type]
                    answer_count=counts.get(q.id, 0),
                    my_option=answer[0] if answer else None,  # type: ignore[arg-type]
                    # Correctness is part of the answer, so it waits too.
                    my_is_correct=answer[1] if answer and reveal else None,
                )
            )
        return reads

    # -- commands --------------------------------------------------------

    async def create(
        self, *, classroom_id: uuid.UUID, user: User, prompt: str, options: list[str], correct: str
    ) -> QuestionRead:
        member = await self._membership(classroom_id, user)
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can run a quiz")

        question = QuizQuestion(
            classroom_id=classroom_id,
            created_by=user.id,
            prompt=prompt,
            options=options,
            correct_option=correct,
            status=QuizStatus.ACTIVE,
            started_at=utcnow(),
        )
        try:
            async with self.db.begin_nested():
                self.db.add(question)
                await self.db.flush()
        except IntegrityError as exc:
            raise ConflictError("End the current question before asking another") from exc

        await PushService(self.db).send_to_classroom(
            classroom_id,
            PushMessage(
                title="Quiz question",
                body=prompt[:120],
                data={"type": "quiz", "classroom_id": str(classroom_id)},
            ),
            roles={MemberRole.STUDENT},
        )

        [read] = await self._to_reads([question], user=user, role=member.role)
        return read

    async def end(self, *, question_id: uuid.UUID, user: User) -> QuestionRead:
        question, member = await self._question_for_member(question_id, user)
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can end a question")
        if question.status is QuizStatus.ACTIVE:
            question.status = QuizStatus.ENDED
            question.ended_at = utcnow()
            await self.db.flush()
        [read] = await self._to_reads([question], user=user, role=member.role)
        return read

    async def answer(self, *, question_id: uuid.UUID, user: User, option: str) -> AnswerResult:
        question, member = await self._question_for_member(question_id, user)
        if member.role is not MemberRole.STUDENT:
            raise PermissionDeniedError("Only students answer questions")
        if question.status is not QuizStatus.ACTIVE:
            raise ConflictError("This question has ended")
        if option not in OPTION_LETTERS:
            raise ConflictError("Answer A, B, C or D")

        now = utcnow()
        answer = QuizAnswer(
            question_id=question.id,
            student_id=user.id,
            option=option,
            is_correct=option == question.correct_option,
            penalty_seconds=max(0, int((now - question.started_at).total_seconds())),
            answered_at=now,
        )
        try:
            async with self.db.begin_nested():
                self.db.add(answer)
                await self.db.flush()
        except IntegrityError as exc:
            raise ConflictError("You have already answered this question") from exc

        return AnswerResult(
            option=option,  # type: ignore[arg-type]
            is_correct=answer.is_correct,
            penalty_seconds=answer.penalty_seconds,
        )

    # -- queries ---------------------------------------------------------

    async def leaderboard(self, classroom_id: uuid.UUID) -> list[LeaderboardEntry]:
        correct = func.count(case((QuizAnswer.is_correct.is_(True), 1)))
        penalty = func.coalesce(
            func.sum(case((QuizAnswer.is_correct.is_(True), QuizAnswer.penalty_seconds))), 0
        )
        rows = await self.db.execute(
            select(
                User.id,
                User.full_name,
                correct.label("correct"),
                func.count().label("answered"),
                penalty.label("penalty"),
            )
            .select_from(QuizAnswer)
            .join(QuizQuestion, QuizQuestion.id == QuizAnswer.question_id)
            .join(User, User.id == QuizAnswer.student_id)
            .where(QuizQuestion.classroom_id == classroom_id)
            .group_by(User.id, User.full_name)
            # Most correct first; ties go to whoever was faster in total. The
            # name is a last, stable tiebreak so the order never flickers
            # between polls.
            .order_by(correct.desc(), penalty.asc(), User.full_name.asc())
        )

        entries: list[LeaderboardEntry] = []
        previous: tuple[int, int] | None = None
        rank = 0
        for position, (sid, name, n_correct, n_answered, n_penalty) in enumerate(
            rows.tuples().all(), start=1
        ):
            key = (int(n_correct), int(n_penalty))
            # Equal score and equal time share a rank ("1, 1, 3").
            if key != previous:
                rank = position
                previous = key
            entries.append(
                LeaderboardEntry(
                    rank=rank,
                    student_id=sid,
                    student_name=name,
                    correct=key[0],
                    answered=int(n_answered),
                    penalty_seconds=key[1],
                )
            )
        return entries

    async def state(self, *, classroom_id: uuid.UUID, user: User) -> QuizState:
        member = await self._membership(classroom_id, user)
        questions = list(
            (
                await self.db.scalars(
                    select(QuizQuestion)
                    .where(QuizQuestion.classroom_id == classroom_id)
                    .order_by(QuizQuestion.started_at.desc())
                    .limit(RECENT_QUESTIONS)
                )
            ).all()
        )
        reads = await self._to_reads(questions, user=user, role=member.role)
        active = next((r for r in reads if r.status is QuizStatus.ACTIVE), None)
        return QuizState(
            active=active,
            recent=[r for r in reads if r.status is QuizStatus.ENDED],
            leaderboard=await self.leaderboard(classroom_id),
            poll_interval_seconds=settings.quiz_poll_interval_seconds,
        )
