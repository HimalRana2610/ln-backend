"""Live quiz bodies."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.quiz import QuizStatus

OptionLetter = Literal["A", "B", "C", "D"]


class QuestionCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    prompt: str = Field(min_length=1, max_length=1000)
    # Exactly four, A-D in order, none blank.
    options: list[Annotated[str, Field(min_length=1, max_length=300)]] = Field(
        min_length=4, max_length=4
    )
    correct_option: OptionLetter


class QuestionRead(BaseModel):
    id: uuid.UUID
    classroom_id: uuid.UUID
    prompt: str
    options: list[str]
    status: QuizStatus
    started_at: datetime
    ended_at: datetime | None
    # Hidden from students while the question is live, so the answer cannot be
    # read out of a network response.
    correct_option: OptionLetter | None
    answer_count: int
    # The viewer's own answer, when they are a student who has answered.
    my_option: OptionLetter | None = None
    my_is_correct: bool | None = None


class AnswerCreate(BaseModel):
    option: OptionLetter


class AnswerResult(BaseModel):
    option: OptionLetter
    is_correct: bool
    penalty_seconds: int


class LeaderboardEntry(BaseModel):
    rank: int
    student_id: uuid.UUID
    student_name: str
    correct: int
    answered: int
    # Sum of response times over correct answers only: answering wrong quickly
    # must not beat answering right slowly.
    penalty_seconds: int


class QuizState(BaseModel):
    """Everything a quiz screen shows, in one poll."""

    active: QuestionRead | None
    recent: list[QuestionRead]
    leaderboard: list[LeaderboardEntry]
    poll_interval_seconds: int
