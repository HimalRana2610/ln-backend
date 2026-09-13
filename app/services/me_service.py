"""The signed-in user's cross-classroom views, and deleting their account."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ValidationFailedError
from app.core.security import verify_password
from app.models.classroom import Classroom, ClassroomMember, MemberRole
from app.models.note import Asset
from app.models.post import ClassroomPost, PostKind, Submission
from app.models.user import User
from app.schemas.me import ToDoItem, ToDoStatus
from app.services import storage_service


def utcnow() -> datetime:
    return datetime.now(UTC)


def todo_status(
    *, due_date: datetime | None, submitted_at: datetime | None, now: datetime
) -> ToDoStatus:
    """Done if handed in; missing if past due without a submission; else assigned.

    Decided on the server's clock. The old client decided it on the phone's,
    so a phone set a day ahead showed work as missing that was not.
    """
    if submitted_at is not None:
        return ToDoStatus.DONE
    if due_date is not None and due_date < now:
        return ToDoStatus.MISSING
    return ToDoStatus.ASSIGNED


class MeService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def todo(self, user: User) -> list[ToDoItem]:
        rows = await self.db.execute(
            select(ClassroomPost, Classroom.name, Submission.submitted_at)
            .join(Classroom, Classroom.id == ClassroomPost.classroom_id)
            .join(
                ClassroomMember,
                and_(
                    ClassroomMember.classroom_id == ClassroomPost.classroom_id,
                    ClassroomMember.user_id == user.id,
                    # Work is assigned to students; a teacher's to-do would be
                    # every assignment they set, all permanently "assigned".
                    ClassroomMember.role == MemberRole.STUDENT,
                ),
            )
            .outerjoin(
                Submission,
                and_(Submission.post_id == ClassroomPost.id, Submission.student_id == user.id),
            )
            .where(ClassroomPost.kind == PostKind.ASSIGNMENT)
            # Soonest due first; undated work after everything with a deadline.
            .order_by(ClassroomPost.due_date.asc().nulls_last(), ClassroomPost.created_at.desc())
        )

        now = utcnow()
        return [
            ToDoItem(
                post_id=post.id,
                classroom_id=post.classroom_id,
                classroom_name=classroom_name,
                title=post.title,
                description=post.description,
                due_date=post.due_date,
                author_name=post.author.full_name,
                created_at=post.created_at,
                submitted_at=submitted_at,
                is_late=(
                    submitted_at is not None
                    and post.due_date is not None
                    and submitted_at > post.due_date
                ),
                status=todo_status(due_date=post.due_date, submitted_at=submitted_at, now=now),
            )
            for post, classroom_name, submitted_at in rows.unique().tuples().all()
        ]

    async def delete_account(self, *, user: User, password: str) -> None:
        if user.password_hash is None or not verify_password(password, user.password_hash):
            # 400, not 401: the session is fine. A 401 tells clients their token
            # expired, and the mobile app would refresh and retry for nothing.
            raise ValidationFailedError("Password is incorrect", code="incorrect_password")

        owned_classrooms = select(Classroom.id).where(Classroom.owner_id == user.id)
        authored_posts = select(ClassroomPost.id).where(ClassroomPost.author_id == user.id)

        # The database cascades every row away. Storage does not know about
        # foreign keys, so every object that loses its row has to be found and
        # deleted first — the Phase 3 lesson. That is:
        #   * anything this user uploaded, anywhere;
        #   * every file in a classroom they own, which is deleted with them;
        #   * students' submissions to posts they wrote in someone else's
        #     classroom, which go when the post does.
        keys = await self.db.scalars(
            select(Asset.storage_key)
            .outerjoin(Submission, Submission.asset_id == Asset.id)
            .where(
                or_(
                    Asset.owner_id == user.id,
                    Asset.classroom_id.in_(owned_classrooms),
                    Submission.post_id.in_(authored_posts),
                )
            )
            .distinct()
        )
        for key in keys.all():
            storage_service.delete_object(key=key)

        # Submission → asset is not a cascade in that direction, so the rows of
        # the objects just deleted are removed explicitly too.
        orphaned = await self.db.scalars(
            select(Asset)
            .join(Submission, Submission.asset_id == Asset.id)
            .where(Submission.post_id.in_(authored_posts), Asset.owner_id != user.id)
        )
        for asset in orphaned.unique().all():
            await self.db.delete(asset)

        await self.db.delete(user)
        await self.db.flush()
