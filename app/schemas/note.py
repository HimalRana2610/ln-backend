"""Note and upload request/response bodies."""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_type
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.note import NoteSourceType, NoteStatus

MAX_SOURCE_TEXT_CHARS = 200_000

# Only formats Gemini accepts, and only ones a lecture would plausibly be in.
ALLOWED_UPLOAD_TYPES = {
    "audio/mpeg",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/aac",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/ogg",
    "audio/flac",
    "application/pdf",
}

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB — roughly a 3-hour recording

# Classroom materials and assignment work: whatever a lecture or a student's
# answer is plausibly made of. Executables and archives of unknown content are
# not on the list — a class page is not a general file host.
ALLOWED_ATTACHMENT_TYPES = ALLOWED_UPLOAD_TYPES | {
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.presentation",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/zip",
    "application/x-zip-compressed",
    "text/plain",
    "text/markdown",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "video/mp4",
    "video/webm",
}

MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024  # 100 MB


class UploadPurpose(enum.StrEnum):
    # Source material for AI note generation. Must be something Gemini reads.
    NOTE = "note"
    # A file on a classroom post or an assignment submission.
    ATTACHMENT = "attachment"


class PresignUploadRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    filename: str = Field(min_length=1, max_length=400)
    content_type: str = Field(min_length=1, max_length=200)
    size_bytes: int | None = Field(default=None, ge=1, le=MAX_UPLOAD_BYTES)
    # Defaults to `note` so clients written for Phase 2 keep working unchanged.
    purpose: UploadPurpose = UploadPurpose.NOTE

    @field_validator("content_type")
    @classmethod
    def _normalise(cls, value: str) -> str:
        return value.split(";")[0].strip().lower()

    @model_validator(mode="after")
    def _supported(self) -> PresignUploadRequest:
        if self.purpose is UploadPurpose.NOTE:
            allowed, limit = ALLOWED_UPLOAD_TYPES, MAX_UPLOAD_BYTES
        else:
            allowed, limit = ALLOWED_ATTACHMENT_TYPES, MAX_ATTACHMENT_BYTES

        if self.content_type not in allowed:
            raise ValueError(f"Unsupported file type. Allowed: {', '.join(sorted(allowed))}")
        if self.size_bytes is not None and self.size_bytes > limit:
            raise ValueError(f"File is too large. The limit is {limit // (1024 * 1024)} MB")
        return self


class PresignUploadResponse(BaseModel):
    asset_id: uuid.UUID
    upload_url: str
    # The client must send exactly this header, or the signature will not match.
    content_type: str
    expires_in: int


class NoteCreate(BaseModel):
    """Start generating a note.

    Exactly one source must be given: `text` for pasted material, `youtube_url`
    for a video, or `asset_id` for an uploaded recording or PDF.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    date: date_type | None = Field(
        default=None, description="Lecture date. Defaults to today."
    )
    title: str | None = Field(
        default=None,
        max_length=300,
        description="Optional. The model's own heading is used when omitted.",
    )

    text: str | None = Field(default=None, max_length=MAX_SOURCE_TEXT_CHARS)
    youtube_url: str | None = Field(default=None, max_length=500)
    asset_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> NoteCreate:
        sources = [
            bool(self.text),
            bool(self.youtube_url),
            self.asset_id is not None,
        ]
        provided = sum(sources)

        if provided == 0:
            raise ValueError("Provide text, a YouTube link, or an uploaded file")
        if provided > 1:
            raise ValueError("Provide only one source")
        return self


class NoteUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=300)
    markdown: str | None = None
    date: date_type | None = None


class NoteSummary(BaseModel):
    """List view — deliberately excludes `markdown`.

    A classroom's notes can be hundreds of kilobytes of Markdown in total, which
    is wasteful to send for a list nobody has opened yet.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    classroom_id: uuid.UUID
    date: date_type
    title: str
    status: NoteStatus
    source_type: NoteSourceType
    author_id: uuid.UUID
    author_name: str
    duration_seconds: int | None
    error_message: str | None
    created_at: datetime


class NoteRead(NoteSummary):
    markdown: str
