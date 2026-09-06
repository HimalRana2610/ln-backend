"""Persisted refresh-token handles.

Only the token's ``jti`` is stored, never the token itself - the JWT is already
signed, so the row exists purely to answer "is this handle still valid?".

Storing them buys three things the old project lacked:

* server-side logout that actually invalidates a session,
* revoking every session for a user at once (password change, stolen device),
* reuse detection - if a token that was already rotated comes back, the whole
  family is revoked because it means someone replayed a stolen token.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from app.models.user import User


class RefreshToken(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "refresh_tokens"
    __table_args__ = (
        Index("ix_refresh_tokens_user_id_revoked_at", "user_id", "revoked_at"),
    )

    jti: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Set when this token is rotated, pointing at its successor. Lets us walk a
    # token family forward when reuse is detected.
    replaced_by_jti: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Coarse device attribution, shown in a "your sessions" screen.
    user_agent: Mapped[str | None] = mapped_column(String(400), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)

    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RefreshToken {self.jti} user={self.user_id}>"
