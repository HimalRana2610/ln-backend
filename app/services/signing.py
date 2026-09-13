"""The signed attendance message. Pure; mirrored in ln-app's `attendance_signer.dart`.

A phone signs this exact text with its bound Ed25519 key:

    ln-attendance-v1
    session:<session uuid>
    user:<user uuid>
    device:<device uuid>
    nonce:<nonce>
    issued_at:<unix seconds>
    biometric:<1 or 0>
    observations:<sha256 hex of the observation lines>

Observation lines are ``window:token:rssi:hop`` joined by ``\\n``, in the order
sent. Hashing the lines rather than JSON avoids two languages having to agree
on how to serialise it byte for byte.

What each field defeats:

* ``session`` / ``user`` — a signature for one lecture or student reused for another.
* ``device`` — a request relayed from a phone other than the bound one.
* ``nonce`` + ``issued_at`` — a captured request replayed later.
* ``observations`` — evidence swapped after signing.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from collections.abc import Iterable, Sequence
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

VERSION = "ln-attendance-v1"
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64


class ObservationLike(Protocol):
    window: int
    token: str
    rssi: int
    hop: int


def observations_digest(observations: Iterable[ObservationLike]) -> str:
    lines = "\n".join(f"{o.window}:{o.token.lower()}:{o.rssi}:{o.hop}" for o in observations)
    return hashlib.sha256(lines.encode()).hexdigest()


def message(
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    device_id: uuid.UUID,
    nonce: str,
    issued_at: int,
    biometric: bool,
    observations: Sequence[ObservationLike],
) -> bytes:
    return "\n".join(
        [
            VERSION,
            f"session:{session_id}",
            f"user:{user_id}",
            f"device:{device_id}",
            f"nonce:{nonce}",
            f"issued_at:{issued_at}",
            f"biometric:{1 if biometric else 0}",
            f"observations:{observations_digest(observations)}",
        ]
    ).encode()


def decode_public_key(value: str) -> bytes | None:
    """Raw 32-byte key from base64, or None if it is not one."""
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if len(raw) == PUBLIC_KEY_BYTES else None


def verify(*, public_key_b64: str, signature_b64: str, signed: bytes) -> bool:
    raw_key = decode_public_key(public_key_b64)
    try:
        signature = base64.b64decode(signature_b64, validate=True)
    except (binascii.Error, ValueError):
        return False
    if raw_key is None or len(signature) != SIGNATURE_BYTES:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, signed)
    except (InvalidSignature, ValueError):
        return False
    return True
