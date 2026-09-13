"""A simulated student phone: an Ed25519 key, a bound device, signed requests.

Builds the signed message with `app.services.signing.message` — the same
function the server verifies with — so the tests pin the server's contract,
while `ln-app`'s own tests pin the Dart implementation against fixed vectors.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.attendance import Observation
from app.services import signing

API = "/api/v1"


@dataclass
class Phone:
    headers: dict[str, str]
    user_id: uuid.UUID
    key: Ed25519PrivateKey = field(default_factory=Ed25519PrivateKey.generate)
    fingerprint: str = field(
        default_factory=lambda: hashlib.sha256(secrets.token_bytes(16)).hexdigest()
    )
    device_id: uuid.UUID | None = None

    @property
    def public_key(self) -> str:
        raw = self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    async def register(self, client: AsyncClient) -> Any:
        response = await client.post(
            f"{API}/devices/register",
            json={
                "public_key": self.public_key,
                "fingerprint_hash": self.fingerprint,
                "platform": "android",
                "model": "Test Phone",
            },
            headers=self.headers,
        )
        if response.status_code == 200:
            self.device_id = uuid.UUID(response.json()["id"])
        return response

    def signed_body(
        self,
        *,
        session_id: uuid.UUID | str,
        observations: list[dict[str, Any]],
        issued_at: int,
        nonce: str | None = None,
        biometric: bool = True,
        device_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        device = device_id or self.device_id
        assert device is not None, "register the phone first"
        nonce = nonce or secrets.token_urlsafe(18)
        message = signing.message(
            session_id=uuid.UUID(str(session_id)),
            user_id=self.user_id,
            device_id=device,
            nonce=nonce,
            issued_at=issued_at,
            biometric=biometric,
            observations=[Observation(**o) for o in observations],
        )
        return {
            "observations": observations,
            "device_id": str(device),
            "nonce": nonce,
            "issued_at": issued_at,
            "signature": base64.b64encode(self.key.sign(message)).decode(),
            "biometric_verified": biometric,
        }


async def verify_email(db: AsyncSession, headers: dict[str, str], client: AsyncClient) -> uuid.UUID:
    me = await client.get(f"{API}/users/me", headers=headers)
    user_id = uuid.UUID(me.json()["id"])
    await db.execute(update(User).where(User.id == user_id).values(is_email_verified=True))
    await db.flush()
    return user_id


async def ready_phone(client: AsyncClient, db: AsyncSession, headers: dict[str, str]) -> Phone:
    """A student who has verified their email and bound a phone."""
    phone = Phone(headers=headers, user_id=await verify_email(db, headers, client))
    response = await phone.register(client)
    assert response.status_code == 200, response.text
    return phone
