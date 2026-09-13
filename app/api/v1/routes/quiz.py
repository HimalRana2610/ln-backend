"""Live quiz endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.quiz import AnswerCreate, AnswerResult, QuestionCreate, QuestionRead, QuizState
from app.services.quiz_service import QuizService

router = APIRouter(tags=["quiz"])


def get_service(db: DbSession) -> QuizService:
    return QuizService(db)


ServiceDep = Annotated[QuizService, Depends(get_service)]


@router.get("/classrooms/{classroom_id}/quiz", response_model=QuizState)
async def quiz_state(
    classroom_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> QuizState:
    """The live question, recent ones and the leaderboard, in one response.

    Poll every `poll_interval_seconds` while the quiz is on screen. A student
    does not see `correct_option` until the question ends.
    """
    return await service.state(classroom_id=classroom_id, user=current_user)


@router.post(
    "/classrooms/{classroom_id}/quiz/questions",
    response_model=QuestionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_question(
    classroom_id: uuid.UUID,
    payload: QuestionCreate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> QuestionRead:
    """Ask a question. Teachers only; one live question per classroom (409)."""
    return await service.create(
        classroom_id=classroom_id,
        user=current_user,
        prompt=payload.prompt,
        options=payload.options,
        correct=payload.correct_option,
    )


@router.post("/quiz/questions/{question_id}/end", response_model=QuestionRead)
async def end_question(
    question_id: uuid.UUID, service: ServiceDep, current_user: CurrentUser
) -> QuestionRead:
    """Close a question to answers and reveal the correct option. Teachers only."""
    return await service.end(question_id=question_id, user=current_user)


@router.post(
    "/quiz/questions/{question_id}/answers",
    response_model=AnswerResult,
    status_code=status.HTTP_201_CREATED,
)
async def answer_question(
    question_id: uuid.UUID,
    payload: AnswerCreate,
    service: ServiceDep,
    current_user: CurrentUser,
) -> AnswerResult:
    """Answer once. Students only; a second answer is 409 and changes nothing."""
    return await service.answer(question_id=question_id, user=current_user, option=payload.option)
