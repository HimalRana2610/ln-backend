"""Classroom posts, assignment submissions and file downloads.

Files never pass through here. A client uploads straight to storage with a
presigned URL, then creates a post or submission naming the asset; downloads are
a presigned URL too. What this service owns is *who may do what* — and making
sure that deleting anything also deletes the objects behind it, which no foreign
key can do.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, PermissionDeniedError
from app.models.classroom import ClassroomMember, MemberRole
from app.models.note import Asset, Note
from app.models.post import ClassroomPost, PostKind, Submission
from app.models.user import User
from app.schemas.post import AssetInfo, DownloadLink, PostRead, SubmissionRead
from app.services import storage_service


class PostService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- access ----------------------------------------------------------

    async def _membership(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> ClassroomMember | None:
        result = await self.db.execute(
            select(ClassroomMember).where(
                ClassroomMember.classroom_id == classroom_id,
                ClassroomMember.user_id == user_id,
            )
        )
        return result.scalar_one_or_none()

    async def _require_membership(
        self, classroom_id: uuid.UUID, user_id: uuid.UUID
    ) -> ClassroomMember:
        member = await self._membership(classroom_id, user_id)
        if member is None:
            # "Not found", not "forbidden": a non-member must not be able to
            # discover that a classroom exists.
            raise NotFoundError("Classroom not found")
        return member

    async def _post_for_member(
        self, post_id: uuid.UUID, user: User
    ) -> tuple[ClassroomPost, MemberRole]:
        post = await self.db.get(ClassroomPost, post_id)
        if post is None:
            raise NotFoundError("Post not found")

        member = await self._membership(post.classroom_id, user.id)
        if member is None:
            # Same message as a missing post, so a non-member learns nothing.
            raise NotFoundError("Post not found")
        return post, member.role

    # -- files -----------------------------------------------------------

    async def _claim_upload(
        self, *, asset_id: uuid.UUID, user: User, classroom_id: uuid.UUID
    ) -> Asset:
        """Attach a finished upload to a classroom, enforcing the storage cap."""
        asset = await self.db.get(Asset, asset_id)
        if asset is None or asset.owner_id != user.id:
            # Ownership stops one user attaching another's upload.
            raise NotFoundError("Upload not found")

        if asset.is_uploaded:
            # An asset belongs to exactly one post or submission. Sharing it
            # would mean deleting either one deletes the other's file.
            raise ConflictError("That file is already attached to something else")

        # Ask storage rather than trusting the client: this confirms the PUT
        # really completed, and measures the size that counts against the cap.
        size = storage_service.object_size(key=asset.storage_key)
        if size is None:
            raise ConflictError("The upload has not finished yet")

        used = await self.db.scalar(
            select(func.coalesce(func.sum(Asset.size_bytes), 0)).where(
                Asset.classroom_id == classroom_id, Asset.is_uploaded.is_(True)
            )
        )
        limit = settings.classroom_storage_limit_mb * 1024 * 1024
        if int(used or 0) + size > limit:
            raise ConflictError(
                f"This classroom has used its {settings.classroom_storage_limit_mb} MB "
                "of file storage. Delete old materials to make room."
            )

        asset.size_bytes = size
        asset.is_uploaded = True
        asset.classroom_id = classroom_id
        return asset

    async def _discard(self, asset: Asset) -> None:
        """Delete an asset row *and* its stored object.

        The object goes first. If that fails the exception rolls the whole
        request back, leaving a row that still points at a real file — rather
        than the reverse, an unreferenced file quietly using up the quota.
        """
        storage_service.delete_object(key=asset.storage_key)
        await self.db.delete(asset)

    @staticmethod
    def _asset_info(asset: Asset | None) -> AssetInfo | None:
        if asset is None:
            return None
        return AssetInfo(
            id=asset.id,
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
        )

    # -- mapping ---------------------------------------------------------

    def _to_read(
        self,
        post: ClassroomPost,
        *,
        submission_count: int | None = None,
        my_submitted_at: datetime | None = None,
    ) -> PostRead:
        return PostRead(
            id=post.id,
            classroom_id=post.classroom_id,
            kind=post.kind,
            title=post.title,
            description=post.description,
            due_date=post.due_date,
            author_id=post.author_id,
            author_name=post.author.full_name,
            asset=self._asset_info(post.asset),
            created_at=post.created_at,
            updated_at=post.updated_at,
            submission_count=submission_count,
            my_submitted_at=my_submitted_at,
        )

    @staticmethod
    def _submission_to_read(submission: Submission, post: ClassroomPost) -> SubmissionRead:
        asset = PostService._asset_info(submission.asset)
        assert asset is not None  # submission.asset_id is NOT NULL
        return SubmissionRead(
            id=submission.id,
            post_id=submission.post_id,
            student_id=submission.student_id,
            student_name=submission.student.full_name,
            student_email=submission.student.email,
            asset=asset,
            submitted_at=submission.submitted_at,
            is_late=post.due_date is not None and submission.submitted_at > post.due_date,
        )

    async def _with_submission_status(
        self, posts: list[ClassroomPost], *, user: User, role: MemberRole
    ) -> list[PostRead]:
        """Map posts, adding the per-viewer assignment fields in two queries."""
        assignment_ids = [p.id for p in posts if p.kind is PostKind.ASSIGNMENT]
        counts: dict[uuid.UUID, int] = {}
        mine: dict[uuid.UUID, datetime] = {}

        if assignment_ids and role.can_edit_classroom:
            rows = await self.db.execute(
                select(Submission.post_id, func.count())
                .where(Submission.post_id.in_(assignment_ids))
                .group_by(Submission.post_id)
            )
            counts = {post_id: int(count) for post_id, count in rows.all()}
        elif assignment_ids:
            own = await self.db.execute(
                select(Submission.post_id, Submission.submitted_at).where(
                    Submission.post_id.in_(assignment_ids), Submission.student_id == user.id
                )
            )
            mine = dict(own.tuples().all())

        reads: list[PostRead] = []
        for post in posts:
            if post.kind is not PostKind.ASSIGNMENT:
                reads.append(self._to_read(post))
            elif role.can_edit_classroom:
                reads.append(self._to_read(post, submission_count=counts.get(post.id, 0)))
            else:
                reads.append(self._to_read(post, my_submitted_at=mine.get(post.id)))
        return reads

    # -- posts -----------------------------------------------------------

    async def list_for_classroom(
        self, *, classroom_id: uuid.UUID, user: User, kind: PostKind | None
    ) -> list[PostRead]:
        member = await self._require_membership(classroom_id, user.id)

        query = select(ClassroomPost).where(ClassroomPost.classroom_id == classroom_id)
        if kind is not None:
            query = query.where(ClassroomPost.kind == kind)

        result = await self.db.execute(query.order_by(ClassroomPost.created_at.desc()))
        posts = list(result.unique().scalars().all())
        return await self._with_submission_status(posts, user=user, role=member.role)

    async def get(self, *, post_id: uuid.UUID, user: User) -> PostRead:
        post, role = await self._post_for_member(post_id, user)
        [read] = await self._with_submission_status([post], user=user, role=role)
        return read

    async def create(
        self,
        *,
        classroom_id: uuid.UUID,
        user: User,
        kind: PostKind,
        title: str,
        description: str | None,
        due_date: datetime | None,
        asset_id: uuid.UUID | None,
    ) -> PostRead:
        member = await self._require_membership(classroom_id, user.id)
        if not member.role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can post to this classroom")

        asset = (
            await self._claim_upload(asset_id=asset_id, user=user, classroom_id=classroom_id)
            if asset_id is not None
            else None
        )

        post = ClassroomPost(
            classroom_id=classroom_id,
            author_id=user.id,
            kind=kind,
            title=title,
            description=description or None,
            due_date=due_date,
            asset_id=asset.id if asset else None,
        )
        self.db.add(post)
        await self.db.flush()
        await self.db.refresh(post)

        [read] = await self._with_submission_status([post], user=user, role=member.role)
        return read

    async def update(
        self, *, post_id: uuid.UUID, user: User, changes: dict[str, object]
    ) -> PostRead:
        post, role = await self._post_for_member(post_id, user)

        if post.author_id != user.id and not role.can_edit_classroom:
            raise PermissionDeniedError("Only the author or a teacher can edit this post")
        if changes.get("due_date") is not None and post.kind is not PostKind.ASSIGNMENT:
            raise ConflictError("Only assignments have a due date")
        if "title" in changes and changes["title"] is None:
            # `title: null` is not "leave it alone" — that is omitting the key.
            raise ConflictError("A post needs a title")

        for field, value in changes.items():
            setattr(post, field, value)
        await self.db.flush()
        # `updated_at` is set by the database, so reload it rather than
        # triggering an implicit lazy load, which async sessions forbid.
        await self.db.refresh(post)

        [read] = await self._with_submission_status([post], user=user, role=role)
        return read

    async def delete(self, *, post_id: uuid.UUID, user: User) -> None:
        post, role = await self._post_for_member(post_id, user)

        if post.author_id != user.id and not role.can_edit_classroom:
            raise PermissionDeniedError("Only the author or a teacher can delete this post")

        # Submissions would go by database cascade, but their *files* would not:
        # storage knows nothing about foreign keys. So every object is deleted
        # explicitly — the post's own file and every student's work.
        submissions = await self.db.execute(
            select(Submission).where(Submission.post_id == post.id)
        )
        for submission in submissions.unique().scalars().all():
            asset = submission.asset
            await self.db.delete(submission)
            await self._discard(asset)

        if post.asset is not None:
            await self._discard(post.asset)

        await self.db.delete(post)
        await self.db.flush()

    # -- submissions -----------------------------------------------------

    async def submit(
        self, *, post_id: uuid.UUID, user: User, asset_id: uuid.UUID
    ) -> SubmissionRead:
        post, role = await self._post_for_member(post_id, user)

        if role is not MemberRole.STUDENT:
            raise PermissionDeniedError("Only students submit work")
        if post.kind is not PostKind.ASSIGNMENT:
            raise ConflictError("Only assignments accept submissions")

        asset = await self._claim_upload(
            asset_id=asset_id, user=user, classroom_id=post.classroom_id
        )
        now = datetime.now(UTC)

        existing = await self.db.scalar(
            select(Submission).where(
                Submission.post_id == post.id, Submission.student_id == user.id
            )
        )

        if existing is None:
            submission = Submission(
                post_id=post.id, student_id=user.id, asset_id=asset.id, submitted_at=now
            )
            self.db.add(submission)
        else:
            # Resubmitting replaces the file on the same row, so a student never
            # has two submissions and the teacher never has to guess which counts.
            previous = existing.asset
            existing.asset_id = asset.id
            existing.submitted_at = now
            submission = existing
            await self.db.flush()
            await self._discard(previous)

        await self.db.flush()
        await self.db.refresh(submission)
        return self._submission_to_read(submission, post)

    async def list_submissions(self, *, post_id: uuid.UUID, user: User) -> list[SubmissionRead]:
        post, role = await self._post_for_member(post_id, user)
        if not role.can_edit_classroom:
            raise PermissionDeniedError("Only teachers can see every submission")

        result = await self.db.execute(
            select(Submission)
            .join(User, User.id == Submission.student_id)
            .where(Submission.post_id == post.id)
            .order_by(User.full_name)
        )
        return [self._submission_to_read(s, post) for s in result.unique().scalars().all()]

    async def my_submission(self, *, post_id: uuid.UUID, user: User) -> SubmissionRead:
        post, _ = await self._post_for_member(post_id, user)

        submission = await self.db.scalar(
            select(Submission).where(
                Submission.post_id == post.id, Submission.student_id == user.id
            )
        )
        if submission is None:
            raise NotFoundError("You have not submitted this assignment")
        return self._submission_to_read(submission, post)

    # -- downloads -------------------------------------------------------

    async def _can_read_asset(self, asset: Asset, user: User) -> bool:
        """Whoever may see the thing a file is attached to may download it."""
        post = await self.db.scalar(select(ClassroomPost).where(ClassroomPost.asset_id == asset.id))
        if post is not None:
            return await self._membership(post.classroom_id, user.id) is not None

        submission = await self.db.scalar(select(Submission).where(Submission.asset_id == asset.id))
        if submission is not None:
            if submission.student_id == user.id:
                return True
            parent = await self.db.get(ClassroomPost, submission.post_id)
            if parent is None:
                return False
            member = await self._membership(parent.classroom_id, user.id)
            return member is not None and member.role.can_edit_classroom

        note = await self.db.scalar(select(Note).where(Note.source_asset_id == asset.id))
        if note is not None:
            return await self._membership(note.classroom_id, user.id) is not None

        # Attached to nothing yet: only the uploader.
        return asset.owner_id == user.id

    async def download_link(self, *, asset_id: uuid.UUID, user: User) -> DownloadLink:
        asset = await self.db.get(Asset, asset_id)
        if asset is None or not await self._can_read_asset(asset, user):
            raise NotFoundError("File not found")

        return DownloadLink(
            url=storage_service.presign_download(key=asset.storage_key, filename=asset.filename),
            filename=asset.filename,
            content_type=asset.content_type,
            size_bytes=asset.size_bytes,
            expires_in=settings.s3_presign_ttl_seconds,
        )
