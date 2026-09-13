"""Push a "due soon" reminder for assignments due within the next day.

    python -m app.reminders

Run it hourly from any scheduler (cron, a GitHub Actions schedule, Render's
cron jobs). Each assignment is reminded once; students who already submitted
are skipped.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionFactory, engine
from app.models.classroom import ClassroomMember, MemberRole
from app.models.post import ClassroomPost, PostKind, Submission
from app.services.push_service import PushMessage, PushService

REMIND_WITHIN = timedelta(hours=24)


async def send_due_reminders(db: AsyncSession, *, now: datetime) -> int:
    """Remind every unreminded assignment due in (now, now + 24h]. Returns the count."""
    posts = (
        await db.scalars(
            select(ClassroomPost).where(
                ClassroomPost.kind == PostKind.ASSIGNMENT,
                ClassroomPost.due_reminder_sent_at.is_(None),
                ClassroomPost.due_date > now,
                ClassroomPost.due_date <= now + REMIND_WITHIN,
            )
        )
    ).unique().all()

    push = PushService(db)
    for post in posts:
        submitted = select(Submission.student_id).where(Submission.post_id == post.id)
        students = await db.scalars(
            select(ClassroomMember.user_id).where(
                ClassroomMember.classroom_id == post.classroom_id,
                ClassroomMember.role == MemberRole.STUDENT,
                ClassroomMember.user_id.not_in(submitted),
            )
        )
        await push.send_to_users(
            students.all(),
            PushMessage(
                title="Due soon",
                body=post.title,
                data={
                    "type": "due_soon",
                    "classroom_id": str(post.classroom_id),
                    "post_id": str(post.id),
                },
            ),
        )
        post.due_reminder_sent_at = now
    await db.flush()
    return len(posts)


async def main() -> None:
    try:
        async with SessionFactory() as session:
            count = await send_due_reminders(session, now=datetime.now(UTC))
            await session.commit()
        print(f"reminded {count} assignment(s)")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
