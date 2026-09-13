"""Stored files and AI-generated lecture notes.

Generation is slow — transcribing a lecture and prompting a model takes minutes,
far longer than any HTTP request should live. So a note is a *record with a
status*, created immediately and filled in later, rather than the return value
of a long call. Clients create it, then poll until it is ready.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date as date_type
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.classroom import Classroom
    from app.models.user import User


class NoteSourceType(enum.StrEnum):
    AUDIO = "audio"
    TEXT = "text"
    PDF = "pdf"
    YOUTUBE = "youtube"


class NoteStatus(enum.StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"

    @property
    def is_terminal(self) -> bool:
        """Whether a client can stop polling."""
        return self in {NoteStatus.READY, NoteStatus.FAILED}


class Asset(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A file in object storage.

    Only the key is stored, never the bytes. Uploads go straight from the client
    to storage with a presigned URL, so a lecture recording never passes through
    the API — which both keeps requests short and avoids paying to proxy it.
    """

    __tablename__ = "assets"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    classroom_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("classrooms.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    storage_key: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    filename: Mapped[str] = mapped_column(String(400), nullable=False)
    content_type: Mapped[str] = mapped_column(String(200), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # False until the client confirms the upload finished. An abandoned upload
    # leaves a row that a cleanup job can find and remove.
    is_uploaded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )

    owner: Mapped[User] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Asset {self.filename}>"


class Note(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "notes"
    __table_args__ = (
        # The dashboard lists a classroom's notes newest-first by lecture date.
        Index("ix_notes_classroom_id_date", "classroom_id", "date"),
        # The worker claims work with `WHERE status = 'pending' ORDER BY created_at`.
        Index("ix_notes_status_created_at", "status", "created_at"),
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

    # The lecture's date, which is not necessarily today — a student may write
    # up a recording days later.
    date: Mapped[date_type] = mapped_column(Date, nullable=False)

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    markdown: Mapped[str] = mapped_column(Text, nullable=False, server_default="")

    source_type: Mapped[NoteSourceType] = mapped_column(
        Enum(
            NoteSourceType,
            name="note_source_type",
            values_callable=lambda e: [m.value for m in e],
        ),
        nullable=False,
    )
    # The raw input: pasted text, or a YouTube URL. Null for uploaded files,
    # which are referenced by `source_asset_id` instead.
    source_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("assets.id", ondelete="SET NULL"), nullable=True
    )

    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus, name="note_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
        server_default=NoteStatus.PENDING.value,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    classroom: Mapped[Classroom] = relationship()
    author: Mapped[User] = relationship(lazy="joined")
    source_asset: Mapped[Asset | None] = relationship(lazy="joined")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Note {self.title!r} {self.status.value}>"
