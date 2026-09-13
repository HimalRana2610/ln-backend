"""The rotating attendance beacon — constants and token maths.

Pure functions, no I/O, so the mobile client can be checked against them byte
for byte (``ln-app``'s ``beacon_codec.dart`` implements the same thing).

**Why a token at all.** The old app advertised a fixed payload whose
"signature" field was the constant ``0xDEADBEEF``, and the server marked anyone
present who asked. A student at home could therefore claim every window of the
lecture. Here each 30-second window has its own token,

    token(w) = HMAC-SHA256(beacon_secret, "<session_id>:<w>")[:8 bytes]

and only the server and the teacher's phone hold ``beacon_secret``. A student
can present a window's token only if some phone in the room heard it — so
claiming 70% of the lecture requires having been reachable for 70% of it.

**What the token does not prove.** A friend in the room could read tokens out
to a student at home in real time. Binding tokens to a device and a biometric
check is Phase 5; this phase closes the offline and replay routes.

**Advertisement layout** (manufacturer-specific data, 17 bytes, big-endian):

    [0-3]   session tag   first 4 bytes of the session UUID
    [4-7]   window index  uint32
    [8]     hop count     0 = teacher, 1-2 = relayed
    [9-16]  token         8 bytes
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import uuid
from datetime import datetime

# -- protocol constants, mirrored in ln-app ------------------------------------

WINDOW_SECONDS = 30
MAX_HOP_DEPTH = 2
REBROADCAST_SECONDS = 30
TOKEN_BYTES = 8

# Share of elapsed windows a student must have been in range for.
REQUIRED_PRESENCE = 0.70

# Bluetooth SIG company id reserved for testing. Harmless for a campus app that
# is never certified; swap for an assigned id if one is ever obtained.
MANUFACTURER_ID = 0xFFFF

# A signal is Strong at `threshold + 10` dBm or above, Medium from `threshold`,
# Weak down to `threshold - 10`, and out of range below that. Only Strong and
# Medium count, and only they may be relayed.
SIGNAL_BAND_DB = 10


def new_secret() -> str:
    return secrets.token_hex(32)


def window_index(*, started_at: datetime, at: datetime) -> int:
    """Which 30-second window `at` falls in. Negative before the session began."""
    return math.floor((at - started_at).total_seconds() / WINDOW_SECONDS)


def token(*, secret: str, session_id: uuid.UUID, window: int) -> str:
    """The window's token as 16 lowercase hex characters."""
    digest = hmac.new(
        bytes.fromhex(secret), f"{session_id}:{window}".encode(), hashlib.sha256
    ).digest()
    return digest[:TOKEN_BYTES].hex()


def token_matches(*, secret: str, session_id: uuid.UUID, window: int, presented: str) -> bool:
    expected = token(secret=secret, session_id=session_id, window=window)
    # Constant-time, so response timing does not leak how many hex digits of a
    # guess were right.
    return hmac.compare_digest(expected, presented.lower())


def session_tag(session_id: uuid.UUID) -> str:
    """First 4 bytes of the session id, as advertised, in hex."""
    return session_id.bytes[:4].hex()


def signal_counts(*, rssi: int, rssi_threshold: int) -> bool:
    """Strong or Medium — the only signals that count for attendance."""
    return rssi >= rssi_threshold


def required_windows(elapsed: int) -> int:
    """Windows in range needed out of `elapsed`, rounding in the strict direction."""
    # round() first: 0.7 * 10 is 7.000000000000001 in binary floating point,
    # and a bare ceil would demand 8 of 10.
    return math.ceil(round(REQUIRED_PRESENCE * elapsed, 9))
