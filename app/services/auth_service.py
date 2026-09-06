"""Authentication use cases.

Pure application logic - no FastAPI imports. Anything HTTP-shaped (status codes,
headers) is decided by the route layer via :mod:`app.core.exceptions`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, ConflictError, NotFoundError
from app.core.security import (
    TokenError,
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from app.models.refresh_token import RefreshToken
from app.models.user import User
from app.schemas.auth import TokenPair


class AuthService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # -- lookups ---------------------------------------------------------

    async def get_by_email(self, email: str) -> User | None:
        result = await self.db.execute(select(User).where(User.email == email.lower()))
        return result.scalar_one_or_none()

    async def get_by_id(self, user_id: uuid.UUID) -> User:
        user = await self.db.get(User, user_id)
        if user is None:
            raise NotFoundError("User not found")
        return user

    # -- registration ----------------------------------------------------

    async def register(
        self, *, email: str, full_name: str, password: str, institute: str | None
    ) -> User:
        if await self.get_by_email(email) is not None:
            raise ConflictError("An account with that email already exists")

        user = User(
            email=email.lower(),
            full_name=full_name,
            institute=institute,
            password_hash=hash_password(password),
        )
        self.db.add(user)
        await self.db.flush()
        return user

    # -- login -----------------------------------------------------------

    async def authenticate(self, *, email: str, password: str) -> User:
        user = await self.get_by_email(email)

        # Verify against a dummy hash when the user is missing so that timing
        # does not reveal which emails are registered.
        if user is None or user.password_hash is None:
            hash_password(password)
            raise AuthenticationError("Incorrect email or password")

        if not verify_password(password, user.password_hash):
            raise AuthenticationError("Incorrect email or password")

        if not user.is_active:
            raise AuthenticationError("This account has been deactivated")

        # Transparently upgrade the hash if argon2 parameters have moved on.
        if password_needs_rehash(user.password_hash):
            user.password_hash = hash_password(password)

        return user

    # -- token issuing ---------------------------------------------------

    async def issue_token_pair(
        self,
        user: User,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> TokenPair:
        access_token, access_expires = create_access_token(
            str(user.id), extra_claims={"email": user.email}
        )
        refresh_token, jti, refresh_expires = create_refresh_token(str(user.id))

        self.db.add(
            RefreshToken(
                jti=jti,
                user_id=user.id,
                expires_at=refresh_expires,
                user_agent=(user_agent or None) and user_agent[:400],
                ip_address=ip_address,
            )
        )
        await self.db.flush()

        expires_in = int((access_expires - datetime.now(UTC)).total_seconds())
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=max(expires_in, 0),
        )

    # -- refresh ---------------------------------------------------------

    async def refresh(
        self,
        raw_token: str,
        *,
        user_agent: str | None = None,
        ip_address: str | None = None,
    ) -> TokenPair:
        """Rotate a refresh token, revoking the one presented.

        If a token that was *already* rotated is presented again, it has been
        replayed - most likely stolen. Every session for that user is then
        revoked rather than just refusing this one request.
        """
        try:
            payload = decode_token(raw_token, "refresh")
        except TokenError as exc:
            raise AuthenticationError(str(exc)) from exc

        jti = str(payload["jti"])
        result = await self.db.execute(select(RefreshToken).where(RefreshToken.jti == jti))
        stored = result.scalar_one_or_none()

        if stored is None:
            raise AuthenticationError("Refresh token is not recognised")

        if stored.revoked_at is not None:
            await self.revoke_all_for_user(stored.user_id)
            raise AuthenticationError("Refresh token has already been used")

        if stored.expires_at <= datetime.now(UTC):
            raise AuthenticationError("Refresh token has expired")

        user = await self.get_by_id(stored.user_id)
        if not user.is_active:
            raise AuthenticationError("This account has been deactivated")

        pair = await self.issue_token_pair(user, user_agent=user_agent, ip_address=ip_address)

        new_payload = decode_token(pair.refresh_token, "refresh")
        stored.revoked_at = datetime.now(UTC)
        stored.replaced_by_jti = str(new_payload["jti"])
        await self.db.flush()

        return pair

    # -- logout ----------------------------------------------------------

    async def revoke(self, raw_token: str) -> None:
        """Revoke a single refresh token. Silent when already gone."""
        try:
            payload = decode_token(raw_token, "refresh")
        except TokenError:
            return

        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.jti == str(payload["jti"]), RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    async def revoke_all_for_user(self, user_id: uuid.UUID) -> None:
        await self.db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )

    # -- access-token resolution ----------------------------------------

    async def user_from_access_token(self, raw_token: str) -> User:
        try:
            payload = decode_token(raw_token, "access")
        except TokenError as exc:
            raise AuthenticationError(str(exc)) from exc

        try:
            user_id = uuid.UUID(str(payload["sub"]))
        except ValueError as exc:
            raise AuthenticationError("Token subject is malformed") from exc

        user = await self.db.get(User, user_id)
        if user is None or not user.is_active:
            raise AuthenticationError("Account is unavailable")
        return user
