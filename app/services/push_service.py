"""Push notifications through Firebase Cloud Messaging.

Sending is best effort. A notification that fails to arrive is an inconvenience;
a teacher's post failing *because* a notification could not be sent would be a
bug. So nothing here raises into the caller.

FCM's HTTP v1 API is called directly — a service-account JWT exchanged for an
OAuth token, then one ``messages:send`` per device — using the ``pyjwt``,
``cryptography`` and ``httpx`` the project already depends on, rather than
pulling in ``firebase-admin`` and its gRPC stack for one POST.

Dead tokens are pruned. FCM answers ``UNREGISTERED`` (404) for an uninstalled
app or a revoked browser permission, and ``INVALID_ARGUMENT`` (400) for a token
that was never valid; every later send to them is wasted time, so they are
deleted as soon as FCM says so.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.classroom import ClassroomMember, MemberRole
from app.models.notification import PushPlatform, PushToken
from app.models.user import User

logger = logging.getLogger("ln.push")

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret


@dataclass(frozen=True)
class PushMessage:
    title: str
    body: str
    # Strings only: FCM's `data` map rejects anything else. Clients use it to
    # open the right screen when the notification is tapped.
    data: Mapping[str, str]


class PushSender(Protocol):
    async def send(self, tokens: list[str], message: PushMessage) -> set[str]:
        """Deliver to each token; return the tokens FCM reported as dead."""
        ...


class LogSender:
    """Used when FCM is not configured, so local development needs no Firebase."""

    async def send(self, tokens: list[str], message: PushMessage) -> set[str]:
        logger.info("push (not configured) to %d device(s): %s", len(tokens), message.title)
        return set()


class FcmSender:
    def __init__(self, service_account: Mapping[str, Any]) -> None:
        self._account = service_account
        self._project_id = settings.fcm_project_id or str(service_account["project_id"])
        self._access_token: str | None = None
        self._expires_at = 0.0

    async def _token(self, client: httpx.AsyncClient) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token

        now = int(time.time())
        assertion = jwt.encode(
            {
                "iss": self._account["client_email"],
                "scope": _SCOPE,
                "aud": _TOKEN_URL,
                "iat": now,
                "exp": now + 3600,
            },
            self._account["private_key"],
            algorithm="RS256",
        )
        response = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": assertion,
            },
        )
        response.raise_for_status()
        body = response.json()
        self._access_token = str(body["access_token"])
        self._expires_at = time.time() + int(body.get("expires_in", 3600))
        return self._access_token

    async def send(self, tokens: list[str], message: PushMessage) -> set[str]:
        dead: set[str] = set()
        url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        async with httpx.AsyncClient(timeout=10) as client:
            headers = {"Authorization": f"Bearer {await self._token(client)}"}
            for token in tokens:
                payload = {
                    "message": {
                        "token": token,
                        "notification": {"title": message.title, "body": message.body},
                        "data": dict(message.data),
                        "android": {"priority": "high"},
                    }
                }
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code in {400, 404} and _is_dead_token(response):
                    dead.add(token)
                elif response.is_error:
                    logger.warning("FCM send failed (%s): %s", response.status_code, response.text)
        return dead


def _is_dead_token(response: httpx.Response) -> bool:
    try:
        error = response.json().get("error", {})
    except ValueError:
        return False
    codes = {str(d.get("errorCode")) for d in error.get("details", []) if isinstance(d, dict)}
    return bool(codes & {"UNREGISTERED", "INVALID_ARGUMENT"}) or error.get("status") == "NOT_FOUND"


def _load_service_account() -> dict[str, Any] | None:
    raw = settings.fcm_service_account_json.strip()
    if not raw:
        return None
    # Either the JSON itself (convenient for a hosting dashboard's env vars) or
    # a path to the downloaded key file (convenient locally).
    text = raw if raw.startswith("{") else Path(raw).read_text(encoding="utf-8")
    account: dict[str, Any] = json.loads(text)
    return account


def _build_sender() -> PushSender:
    try:
        account = _load_service_account()
    except (OSError, ValueError):
        logger.exception("FCM_SERVICE_ACCOUNT_JSON is set but unreadable; pushes disabled")
        return LogSender()
    return FcmSender(account) if account else LogSender()


# Module level, so tests can swap in a fake with monkeypatch.
sender: PushSender = _build_sender()


def utcnow() -> datetime:
    return datetime.now(UTC)


class PushService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def register(self, *, user: User, token: str, platform: PushPlatform) -> None:
        existing = await self.db.scalar(select(PushToken).where(PushToken.token == token))
        if existing is None:
            self.db.add(
                PushToken(user_id=user.id, token=token, platform=platform, last_seen_at=utcnow())
            )
        else:
            # A shared browser or a phone handed on: the token follows whoever
            # signed in last, so the previous account stops receiving pushes.
            existing.user_id = user.id
            existing.platform = platform
            existing.last_seen_at = utcnow()
        await self.db.flush()

    async def unregister(self, *, user: User, token: str) -> None:
        await self.db.execute(
            delete(PushToken).where(PushToken.token == token, PushToken.user_id == user.id)
        )
        await self.db.flush()

    async def send_to_users(self, user_ids: Iterable[uuid.UUID], message: PushMessage) -> None:
        ids = list(set(user_ids))
        if not ids:
            return
        tokens = list(
            (await self.db.scalars(select(PushToken.token).where(PushToken.user_id.in_(ids)))).all()
        )
        if not tokens:
            return

        try:
            dead = await sender.send(tokens, message)
        except Exception:
            # Deliberately broad: a failed push must never fail the request.
            logger.exception("push delivery failed")
            return

        if dead:
            await self.db.execute(delete(PushToken).where(PushToken.token.in_(dead)))
            await self.db.flush()
            logger.info("pruned %d dead push token(s)", len(dead))

    async def send_to_classroom(
        self,
        classroom_id: uuid.UUID,
        message: PushMessage,
        *,
        roles: set[MemberRole] | None = None,
        exclude: uuid.UUID | None = None,
    ) -> None:
        query = select(ClassroomMember.user_id).where(ClassroomMember.classroom_id == classroom_id)
        if roles is not None:
            query = query.where(ClassroomMember.role.in_(roles))
        user_ids = [uid for uid in (await self.db.scalars(query)).all() if uid != exclude]
        await self.send_to_users(user_ids, message)
