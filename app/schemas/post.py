"""Classroom post, submission and download bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from app.models.post import PostKind

MAX_DESCRIPTION_CHARS = 20_000


class AssetInfo(BaseModel):
    """What a client needs to show a file, without being able to fetch it.

    The download URL is deliberately absent: presigned URLs expire, so one is
    issued per click by `GET /assets/{id}/download` and never stored or listed.
    """

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int | None


class PostCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    kind: PostKind
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    # Timezone required. A naive "23:59" is ambiguous between the teacher's
    # zone, the server's and the student's — exactly the bug this field exists
    # to avoid.
    due_date: AwareDatetime | None = None
    asset_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> PostCreate:
        if self.due_date is not None and self.kind is not PostKind.ASSIGNMENT:
            raise ValueError("Only assignments have a due date")
        if self.kind is PostKind.MATERIAL and self.asset_id is None:
            raise ValueError("A material needs a file")
        return self


class PostUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_CHARS)
    due_date: AwareDatetime | None = None


class PostRead(BaseModel):
    id: uuid.UUID
    classroom_id: uuid.UUID
    kind: PostKind
    title: str
    description: str | None
    due_date: datetime | None
    author_id: uuid.UUID
    author_name: str
    asset: AssetInfo | None
    created_at: datetime
    updated_at: datetime

    # Assignments only; null on other kinds. Which of the two is filled depends
    # on who is asking — a teacher gets the count, a student their own status.
    submission_count: int | None = None
    my_submitted_at: datetime | None = None


class SubmissionCreate(BaseModel):
    asset_id: uuid.UUID


class SubmissionRead(BaseModel):
    id: uuid.UUID
    post_id: uuid.UUID
    student_id: uuid.UUID
    student_name: str
    student_email: str
    asset: AssetInfo
    submitted_at: datetime
    is_late: bool


class DownloadLink(BaseModel):
    url: str
    filename: str
    content_type: str
    size_bytes: int | None
    expires_in: int
