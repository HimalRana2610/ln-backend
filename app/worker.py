"""Standalone note-generation worker.

    python -m app.worker

Exists because note generation cannot run on a serverless host. Vercel kills a
function the moment it returns its response, so `BackgroundTasks` — which the
API uses when `NOTES_INLINE_WORKER=true` — never completes there.

Deploying free therefore looks like this:

* **Vercel** runs the API with `NOTES_INLINE_WORKER=false`. Requests stay fast.
* **Render's free tier** runs this worker, which has no per-request time limit.

Both read the same database and the same `notes` table, so a note created by the
API is picked up here within a second or two. On a single long-running server
neither is needed: leave `NOTES_INLINE_WORKER=true` and do not run this at all.

Several workers may run at once — claiming uses `SKIP LOCKED`.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from types import FrameType

from app.db.session import SessionFactory, engine
from app.services.note_service import NoteService

logger = logging.getLogger("ln.worker")

# How long to wait when there was nothing to do. Short enough that a student
# does not notice, long enough not to hammer a free-tier database.
IDLE_SLEEP_SECONDS = 3.0
ERROR_SLEEP_SECONDS = 10.0

_shutdown = asyncio.Event()


def _request_shutdown(signum: int, _frame: FrameType | None) -> None:
    logger.info("received signal %s, finishing current note then stopping", signum)
    _shutdown.set()


async def _run_once() -> bool:
    """Claim and process one note. Returns True when work was done."""
    async with SessionFactory() as session:
        service = NoteService(session)

        note_id = await service.claim_next_pending()
        if note_id is None:
            await session.commit()
            return False

        # Commit the claim before the slow part, so a crash mid-generation
        # leaves the note visibly `processing` rather than silently `pending`.
        # The staleness window then returns it to the queue.
        await session.commit()

        logger.info("generating note %s", note_id)
        await service.process(note_id)
        await session.commit()
        logger.info("finished note %s", note_id)
        return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    logger.info("worker started")

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_shutdown)
        except (ValueError, OSError):  # pragma: no cover - not all platforms
            pass

    try:
        while not _shutdown.is_set():
            try:
                did_work = await _run_once()
            except Exception:  # a worker must outlive one bad note
                logger.exception("worker iteration failed")
                await asyncio.sleep(ERROR_SLEEP_SECONDS)
                continue

            if not did_work:
                await asyncio.sleep(IDLE_SLEEP_SECONDS)
    finally:
        await engine.dispose()
        logger.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
