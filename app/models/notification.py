"""Push notification tokens.

One row per device that can receive pushes. A token is issued by Firebase to an
app install or a browser profile, so it identifies a device, not a person — the
same token signing in as someone else moves to that account.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class PushPlatform(enum.StrEnum):
    WEB = "web"
    ANDROID = "android"
    IOS = "ios"


class PushToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "push_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # FCM tokens run to ~160 characters; the headroom is for format changes.
    token: Mapped[str] = mapped_column(String(512), unique=True, nullable=False)
    platform: Mapped[PushPlatform] = mapped_column(
        Enum(PushPlatform, name="push_platform", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
