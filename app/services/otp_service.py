"""Email verification codes.

Six digits, ten minutes, five tries, and a resend limit so the endpoint cannot
be used to send unlimited mail to anyone's inbox.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import ConflictError, RateLimitedError, ValidationFailedError
from app.models.security import OtpCode, OtpPurpose
from app.models.user import User
from app.services import email_service

CODE_DIGITS = 6


def utcnow() -> datetime:
    """The service's clock. Tests replace it to expire codes."""
    return datetime.now(UTC)


def hash_code(*, user_id: uuid.UUID, purpose: OtpPurpose, code: str) -> str:
    material = f"{user_id}:{purpose.value}:{code}".encode()
    return hmac.new(settings.secret_key.encode(), material, hashlib.sha256).hexdigest()


def new_code() -> str:
    return f"{secrets.randbelow(10**CODE_DIGITS):0{CODE_DIGITS}d}"


class OtpService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def send_email_verification(self, user: User) -> int:
        """Issue and email a code. Returns seconds until another may be sent."""
        if user.is_email_verified:
            raise ConflictError("Your email is already verified")

        purpose = OtpPurpose.EMAIL_VERIFICATION
        now = utcnow()

        last = await self.db.scalar(
            select(func.max(OtpCode.created_at)).where(
                OtpCode.user_id == user.id, OtpCode.purpose == purpose
            )
        )
        cooldown = settings.otp_resend_cooldown_seconds
        if last is not None and (now - last).total_seconds() < cooldown:
            wait = cooldown - int((now - last).total_seconds())
            raise RateLimitedError(
                f"Wait {wait} seconds before asking for another code", retry_after=wait
            )

        recent = await self.db.scalar(
            select(func.count()).where(
                OtpCode.user_id == user.id,
                OtpCode.purpose == purpose,
                OtpCode.created_at > now - timedelta(hours=1),
            )
        )
        if int(recent or 0) >= settings.otp_max_per_hour:
            raise RateLimitedError(
                "Too many codes requested. Try again in an hour.", retry_after=3600
            )

        # Only the newest code works: sending again retires the old ones.
        await self.db.execute(
            update(OtpCode)
            .where(
                OtpCode.user_id == user.id,
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
            )
            .values(consumed_at=now)
        )

        code = new_code()
        self.db.add(
            OtpCode(
                user_id=user.id,
                purpose=purpose,
                code_hash=hash_code(user_id=user.id, purpose=purpose, code=code),
                expires_at=now + timedelta(minutes=settings.otp_ttl_minutes),
                # Explicit, so the cooldown is measured on the same clock as
                # everything else here rather than the database's.
                created_at=now,
            )
        )
        await self.db.flush()

        await email_service.send(
            to=user.email,
            subject=f"{code} is your LectureNote verification code",
            body=(
                f"Hello {user.full_name},\n\n"
                f"Your verification code is {code}. It expires in "
                f"{settings.otp_ttl_minutes} minutes.\n\n"
                "If you did not create a LectureNote account, ignore this email."
            ),
        )
        return cooldown

    async def verify_email(self, user: User, code: str) -> None:
        if user.is_email_verified:
            return

        purpose = OtpPurpose.EMAIL_VERIFICATION
        now = utcnow()
        otp = await self.db.scalar(
            select(OtpCode)
            .where(
                OtpCode.user_id == user.id,
                OtpCode.purpose == purpose,
                OtpCode.consumed_at.is_(None),
            )
            .order_by(OtpCode.created_at.desc())
            .limit(1)
        )

        if otp is None or otp.expires_at <= now:
            raise ValidationFailedError(
                "That code has expired. Ask for a new one.", code="otp_expired"
            )
        if otp.attempts >= settings.otp_max_attempts:
            raise ValidationFailedError(
                "Too many wrong attempts. Ask for a new code.", code="otp_locked"
            )

        expected = hash_code(user_id=user.id, purpose=purpose, code=code)
        if not hmac.compare_digest(expected, otp.code_hash):
            otp.attempts += 1
            remaining = settings.otp_max_attempts - otp.attempts
            # Committed before raising. The request dependency rolls back on
            # any error, and a rolled-back attempt counter would make the limit
            # meaningless — a guesser could try all million codes.
            await self.db.commit()
            if remaining <= 0:
                raise ValidationFailedError(
                    "Too many wrong attempts. Ask for a new code.", code="otp_locked"
                )
            raise ValidationFailedError(
                f"That code is not right. {remaining} attempt{'s' if remaining != 1 else ''} left.",
                code="otp_invalid",
            )

        otp.consumed_at = now
        user.is_email_verified = True
        await self.db.flush()
