"""Find stored files that no database row refers to.

    python -m app.reconcile_storage            # report only
    python -m app.reconcile_storage --delete   # remove the orphans

Every delete path in the API removes its objects explicitly, but an object can
still be orphaned: a request that deleted the object and then failed to commit,
a manual database edit, or an upload whose `assets` row was rolled back after
the client's PUT had already landed. Nothing would ever notice — the free tier
would just fill up. This is the thing that notices.

Also reports asset rows that never finished uploading and are older than a day,
since those are abandoned uploads (a closed tab mid-PUT).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.session import SessionFactory, engine
from app.models.note import Asset
from app.services import storage_service

ABANDONED_AFTER = timedelta(days=1)


async def reconcile(*, delete: bool) -> int:
    async with SessionFactory() as session:
        known = set((await session.scalars(select(Asset.storage_key))).all())

        orphans = [key for key in storage_service.list_keys() if key not in known]
        for key in orphans:
            print(f"orphaned object  {key}")
            if delete:
                storage_service.delete_object(key=key)

        cutoff = datetime.now(UTC) - ABANDONED_AFTER
        abandoned = (
            await session.scalars(
                select(Asset).where(Asset.is_uploaded.is_(False), Asset.created_at < cutoff)
            )
        ).all()
        for asset in abandoned:
            print(f"abandoned upload {asset.storage_key} ({asset.filename})")
            if delete:
                storage_service.delete_object(key=asset.storage_key)
                await session.delete(asset)

        await session.commit()

    action = "deleted" if delete else "found"
    print(f"{action} {len(orphans)} orphaned object(s), {len(abandoned)} abandoned upload(s)")
    return len(orphans) + len(abandoned)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--delete", action="store_true", help="remove what is found")
    args = parser.parse_args()
    try:
        await reconcile(delete=args.delete)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
