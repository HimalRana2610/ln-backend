"""Note use cases: creation, generation, and the status machine.

Generation is deliberately decoupled from the request that starts it. A note is
created as `pending` and returned immediately; something else moves it to
`ready` or `failed` later. That "something else" is either an inline background
task (long-running server) or a separate worker process (serverless) — see
:mod:`app.worker`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.classroom import ClassroomMember, MemberRole
from app.models.note import Asset, Note, NoteSourceType, NoteStatus
from app.models.user import User
from app.schemas.note import NoteRead, NoteSummary
from app.services import ai_service, storage_service


class NoteService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- access ----------------------------------------------------------

    async def _membership(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> ClassroomMember:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.user_id == user_id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            # "Not found", not "forbidden": a non-member must not be able to
            # discover that a classroom exists.
            raise NotFoundError("Classroom not found")
        return member

    async def _note_for_member(self, note_id: uuid.UUID, user: User) -> tuple[Note, MemberRole]:
        note = await self.db.get(Note, note_id)
        if note is None:
            raise NotFoundError("Note not found")

        membership = await self._membership(note.classroom_id, user.id)
        return note, membership.role

    @staticmethod
    def _to_summary(note: Note) -> NoteSummary:
        return NoteSummary(
            id=note.id,
            classroom_id=note.classroom_id,
            date=note.date,
            title=note.title,
            status=note.status,
            source_type=note.source_type,
            author_id=note.author_id,
            author_name=note.author.full_name,
            duration_seconds=note.duration_seconds,
            error_message=note.error_message,
            created_at=note.created_at,
        )

    @classmethod
    def _to_read(cls, note: Note) -> NoteRead:
        return NoteRead(**cls._to_summary(note).model_dump(), markdown=note.markdown)

    # -- uploads ---------------------------------------------------------

    async def create_upload_slot(
        self, *, user: User, filename: str, content_type: str, size_bytes: int | None
    ) -> tuple[Asset, str]:
        """Register an asset and hand back a presigned PUT URL."""
        key = storage_service.build_key(owner_id=user.id, filename=filename)

        asset = Asset(
            owner_id=user.id,
            storage_key=key,
            filename=filename,
            content_type=content_type,
            size_bytes=size_bytes,
        )
        self.db.add(asset)
        await self.db.flush()

        return asset, storage_service.presign_upload(key=key, content_type=content_type)

    # -- creation --------------------------------------------------------

    async def create(
        self,
        *,
        classroom_id: uuid.UUID,
        user: User,
        lecture_date: date | None,
        title: str | None,
        text: str | None,
        youtube_url: str | None,
        asset_id: uuid.UUID | None,
    ) -> NoteRead:
        await self._membership(classroom_id, user.id)

        source_type: NoteSourceType
        source_text: str | None = None
        resolved_asset_id: uuid.UUID | None = None

        if text:
            source_type = NoteSourceType.TEXT
            source_text = text
        elif youtube_url:
            if ai_service.extract_youtube_id(youtube_url) is None:
                raise ConflictError("That does not look like a YouTube video link")
            source_type = NoteSourceType.YOUTUBE
            source_text = youtube_url
        else:
            asset = await self.db.get(Asset, asset_id)
            if asset is None or asset.owner_id != user.id:
                # Checking ownership stops one user attaching another's upload.
                raise NotFoundError("Upload not found")

            source_type = (
                NoteSourceType.PDF
                if asset.content_type == "application/pdf"
                else NoteSourceType.AUDIO
            )
            resolved_asset_id = asset.id

            # The client PUTs straight to storage, so this is the first moment
            # the API learns the upload finished.
            asset.is_uploaded = True
            asset.classroom_id = classroom_id

        note = Note(
            classroom_id=classroom_id,
            author_id=user.id,
            date=lecture_date or datetime.now(UTC).date(),
            title=title or "Generating notes…",
            markdown="",
            source_type=source_type,
            source_text=source_text,
            source_asset_id=resolved_asset_id,
            status=NoteStatus.PENDING,
        )
        self.db.add(note)
        await self.db.flush()

        # Deliberate exception to "routes and services never commit".
        #
        # Generation runs in a *different* session — a background task, or the
        # standalone worker. FastAPI closes `yield` dependencies only after
        # background tasks have run, so `get_db`'s commit happens too late: the
        # task queries for a row that is still inside an uncommitted
        # transaction, finds nothing, and the note sits on `pending` for ever.
        # Retrying does not help, because the commit cannot happen until the
        # task it is waiting on has finished.
        #
        # Creating a note is a complete unit of work in its own right, so
        # committing it here is correct as well as necessary.
        await self.db.commit()
        await self.db.refresh(note)

        return self._to_read(note)

    # -- generation ------------------------------------------------------

    async def claim_next_pending(self) -> uuid.UUID | None:
        """Atomically take one pending note, returning its id.

        `SKIP LOCKED` lets several workers run without two of them picking up
        the same note. Notes stuck in `processing` past the staleness window are
        reclaimed, because a worker can be killed mid-job at any time.
        """
        stale_before = datetime.now(UTC) - timedelta(minutes=settings.notes_stale_after_minutes)

        candidate = (
            select(Note.id)
            .where(
                (Note.status == NoteStatus.PENDING)
                | (
                    (Note.status == NoteStatus.PROCESSING)
                    & (Note.processing_started_at < stale_before)
                )
            )
            .order_by(Note.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        result = await self.db.execute(
            update(Note)
            .where(Note.id.in_(candidate))
            .values(status=NoteStatus.PROCESSING, processing_started_at=datetime.now(UTC))
            .returning(Note.id)
        )
        return result.scalar_one_or_none()

    async def process(self, note_id: uuid.UUID) -> None:
        """Generate the note's content. Never raises — failure is a status."""
        note = await self.db.get(Note, note_id)
        if note is None:
            return

        try:
            generated = await self._generate(note)
        except Exception as exc:  # noqa: BLE001 - any failure must be recorded, not raised
            note.status = NoteStatus.FAILED
            note.error_message = str(exc)[:1000]
            note.completed_at = datetime.now(UTC)
            await self.db.flush()
            return

        note.title = generated.title
        note.markdown = generated.markdown
        note.status = NoteStatus.READY
        note.error_message = None
        note.completed_at = datetime.now(UTC)
        await self.db.flush()

    async def _generate(self, note: Note) -> ai_service.GeneratedNote:
        fallback = f"Notes for {note.date.isoformat()}"

        match note.source_type:
            case NoteSourceType.TEXT:
                return await ai_service.generate_from_text(
                    note.source_text or "", fallback_title=fallback
                )

            case NoteSourceType.YOUTUBE:
                transcript = await ai_service.fetch_youtube_transcript(note.source_text or "")
                return await ai_service.generate_from_text(transcript, fallback_title=fallback)

            case NoteSourceType.PDF | NoteSourceType.AUDIO:
                asset = await self.db.get(Asset, note.source_asset_id)
                if asset is None:
                    raise NotFoundError("The uploaded file is no longer available")

                data = storage_service.download_bytes(key=asset.storage_key)

                if note.source_type is NoteSourceType.PDF:
                    return await ai_service.generate_from_pdf(data, fallback_title=fallback)
                return await ai_service.generate_from_audio(
                    data, mime_type=asset.content_type, fallback_title=fallback
                )

    # -- queries ---------------------------------------------------------

    async def list_for_classroom(
        self, *, classroom_id: uuid.UUID, user: User, on_date: date | None = None
    ) -> list[NoteSummary]:
        await self._membership(classroom_id, user.id)

        query = select(Note).where(Note.classroom_id == classroom_id)
        if on_date is not None:
            query = query.where(Note.date == on_date)

        result = await self.db.execute(query.order_by(Note.date.desc(), Note.created_at.desc()))
        return [self._to_summary(note) for note in result.scalars().all()]

    async def get(self, *, note_id: uuid.UUID, user: User) -> NoteRead:
        note, _ = await self._note_for_member(note_id, user)
        return self._to_read(note)

    # -- mutation --------------------------------------------------------

    async def update(
        self, *, note_id: uuid.UUID, user: User, changes: dict[str, object]
    ) -> NoteRead:
        note, role = await self._note_for_member(note_id, user)

        if note.author_id != user.id and not role.can_edit_classroom:
            raise PermissionDeniedError("Only the author or a teacher can edit this note")

        for field, value in changes.items():
            setattr(note, field, value)
        await self.db.flush()

        return self._to_read(note)

    async def delete(self, *, note_id: uuid.UUID, user: User) -> None:
        note, role = await self._note_for_member(note_id, user)

        if note.author_id != user.id and not role.can_edit_classroom:
            raise PermissionDeniedError("Only the author or a teacher can delete this note")

        # Remove the stored object explicitly. The foreign key cascade deletes
        # the asset row, but storage knows nothing about foreign keys, and an
        # orphaned object silently consumes the free tier forever.
        if note.source_asset is not None:
            storage_service.delete_object(key=note.source_asset.storage_key)
            await self.db.delete(note.source_asset)

        await self.db.delete(note)
        await self.db.flush()
