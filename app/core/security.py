"""Password hashing and JWT issuing/verification.

Two token types are issued:

* **access**  - short lived (minutes), sent as ``Authorization: Bearer``.
* **refresh** - long lived (days), exchanged for a new pair. Every refresh token
  carries a ``jti`` that is persisted, so a token can be revoked server side and
  reuse can be detected. See :mod:`app.services.auth_service`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import settings

TokenType = Literal["access", "refresh"]


def _build_hasher() -> PasswordHasher:
    """Argon2 hasher, deliberately weakened under ENVIRONMENT=test.

    Argon2 is memory-hard by design, which is the point in production and a
    problem in a test suite: every signup costs ~100ms, and a suite that takes
    two minutes is a suite people stop running. The parameters below are the
    library minimums and are used *only* when ENVIRONMENT=test, so production
    strength is unaffected.
    """
    if settings.environment == "test":
        return PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    return PasswordHasher()


_hasher = _build_hasher()


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def password_needs_rehash(password_hash: str) -> bool:
    """True when argon2 parameters have changed since this hash was written."""
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TokenError(Exception):
    """Raised when a token is malformed, expired or of an unexpected type."""


def _create_token(
    subject: str,
    token_type: TokenType,
    expires_delta: timedelta,
    *,
    jti: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str, datetime]:
    """Return ``(encoded_token, jti, expires_at)``."""
    now = datetime.now(UTC)
    expires_at = now + expires_delta
    token_id = jti or str(uuid.uuid4())

    payload: dict[str, Any] = {
        "sub": subject,
        "type": token_type,
        "jti": token_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)

    encoded = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return encoded, token_id, expires_at


def create_access_token(
    subject: str, extra_claims: dict[str, Any] | None = None
) -> tuple[str, datetime]:
    token, _, expires_at = _create_token(
        subject,
        "access",
        timedelta(minutes=settings.access_token_ttl_minutes),
        extra_claims=extra_claims,
    )
    return token, expires_at


def create_refresh_token(subject: str) -> tuple[str, str, datetime]:
    """Return ``(token, jti, expires_at)`` - persist the jti to allow revocation."""
    return _create_token(subject, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode and validate a token, raising :class:`TokenError` on any problem."""
    try:
        payload: dict[str, Any] = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "jti", "type"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("Token has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("Token is invalid") from exc

    if payload.get("type") != expected_type:
        raise TokenError(f"Expected a {expected_type} token")

    return payload
