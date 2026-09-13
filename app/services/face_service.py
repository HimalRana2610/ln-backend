"""Face enrolment and matching.

Photos arrive, are turned into embeddings by the configured service, and are
dropped. Only vectors are stored, and a person can delete theirs at any time.

The old app did two things this deliberately does not:

* It fell back to colour histograms when the embedding service was down. Two
  people in the same lighting then "matched", so a failure silently became a
  pass. Here an unavailable service is an error.
* It sent photos to a personal Hugging Face Space hard-coded in the source.
  Here the service is configuration — whoever deploys chooses, and knows,
  where students' photos go.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import NotFoundError, ServiceUnavailableError, ValidationFailedError
from app.models.security import FaceEnrollment
from app.models.user import User
from app.schemas.security import FaceMatch, FaceStatus

POSES = ("front", "left", "right")
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MIN_DIMENSIONS, MAX_DIMENSIONS = 64, 4096


def is_available() -> bool:
    return bool(settings.face_embedding_url)


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError("Embeddings have different dimensions")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def validate_embedding(value: object) -> list[float]:
    if not isinstance(value, list) or not MIN_DIMENSIONS <= len(value) <= MAX_DIMENSIONS:
        raise ValidationFailedError("No usable face was found in the photo", code="no_face")
    vector = [float(x) for x in value if isinstance(x, int | float)]
    if len(vector) != len(value) or not any(vector) or not all(math.isfinite(x) for x in vector):
        raise ValidationFailedError("No usable face was found in the photo", code="no_face")
    return vector


async def embed(image: bytes, *, content_type: str) -> list[float]:
    """One photo to one embedding. Replaced in tests."""
    if not is_available():
        raise ServiceUnavailableError("Face recognition is not set up on this server")
    if not image or len(image) > MAX_IMAGE_BYTES:
        raise ValidationFailedError("Photos must be under 5 MB", code="image_too_large")

    headers = (
        {"Authorization": f"Bearer {settings.face_embedding_api_key}"}
        if settings.face_embedding_api_key
        else {}
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                settings.face_embedding_url,
                files={"file": ("face.jpg", image, content_type)},
                headers=headers,
            )
    except httpx.HTTPError as exc:
        raise ServiceUnavailableError("The face recognition service could not be reached") from exc

    if response.status_code >= 500:
        raise ServiceUnavailableError("The face recognition service failed")
    try:
        body = response.json()
    except ValueError as exc:
        raise ServiceUnavailableError("The face recognition service returned nonsense") from exc
    if response.status_code >= 400 or not isinstance(body, dict) or "embedding" not in body:
        raise ValidationFailedError("No usable face was found in the photo", code="no_face")
    return validate_embedding(body["embedding"])


class FaceService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def _enrollment(self, user: User) -> FaceEnrollment | None:
        result = await self.db.execute(
            select(FaceEnrollment).where(FaceEnrollment.user_id == user.id)
        )
        return result.scalar_one_or_none()

    async def status(self, user: User) -> FaceStatus:
        enrollment = await self._enrollment(user)
        return FaceStatus(
            available=is_available(),
            enrolled=enrollment is not None,
            enrolled_at=enrollment.created_at if enrollment else None,
        )

    async def enroll(
        self, *, user: User, images: dict[str, tuple[bytes, str]], consent: bool
    ) -> FaceStatus:
        if not consent:
            raise ValidationFailedError(
                "Face enrolment needs your consent to store a face embedding",
                code="consent_required",
            )
        if set(images) != set(POSES):
            raise ValidationFailedError(
                "Enrolment needs front, left and right photos", code="poses_required"
            )

        embeddings = {
            pose: await embed(data, content_type=ctype) for pose, (data, ctype) in images.items()
        }
        if len({len(v) for v in embeddings.values()}) != 1:
            raise ServiceUnavailableError(
                "The face recognition service returned inconsistent results"
            )

        enrollment = await self._enrollment(user)
        now = datetime.now(UTC)
        if enrollment is None:
            enrollment = FaceEnrollment(
                user_id=user.id,
                embeddings=embeddings,
                model_version=settings.face_model_version,
                consented_at=now,
            )
            self.db.add(enrollment)
        else:
            enrollment.embeddings = embeddings
            enrollment.model_version = settings.face_model_version
            enrollment.consented_at = now
        await self.db.flush()
        await self.db.refresh(enrollment)
        return await self.status(user)

    async def verify(self, *, user: User, image: bytes, content_type: str) -> FaceMatch:
        enrollment = await self._enrollment(user)
        if enrollment is None:
            raise NotFoundError("You have not enrolled your face")
        if enrollment.model_version != settings.face_model_version:
            # Embeddings from different models are not comparable at all.
            raise ValidationFailedError(
                "Your face enrolment is from an older model. Enrol again.", code="reenroll_required"
            )

        fresh = await embed(image, content_type=content_type)
        stored = [validate_embedding(enrollment.embeddings[p]) for p in POSES]
        if any(len(s) != len(fresh) for s in stored):
            raise ValidationFailedError(
                "Your face enrolment needs redoing. Enrol again.", code="reenroll_required"
            )

        score = max(cosine_similarity(fresh, s) for s in stored)
        threshold = settings.face_match_threshold
        return FaceMatch(matched=score >= threshold, score=round(score, 4), threshold=threshold)

    async def delete(self, user: User) -> None:
        enrollment = await self._enrollment(user)
        if enrollment is not None:
            await self.db.delete(enrollment)
            await self.db.flush()
