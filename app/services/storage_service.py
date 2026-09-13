"""S3-compatible object storage.

Identical code runs against MinIO locally and Cloudflare R2 or Supabase Storage
in production — only the endpoint and credentials change.

Clients upload **directly** to storage using a presigned URL. A lecture
recording is tens of megabytes; routing it through the API would occupy a
request for minutes and, on a serverless host, exceed the function timeout
outright.
"""

from __future__ import annotations

import uuid
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from app.core.config import settings
from app.core.exceptions import AppError


class StorageError(AppError):
    code = "storage_error"
    message = "File storage is unavailable"


@lru_cache
def _client() -> Any:
    """A cached boto3 client.

    Creating one parses botocore's JSON service model, which is slow enough to
    matter on a cold serverless start.
    """
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id or None,
        aws_secret_access_key=settings.s3_secret_access_key or None,
        # SigV4 is required by R2 and by MinIO's newer releases.
        config=Config(signature_version="s3v4"),
    )


def build_key(*, owner_id: uuid.UUID, filename: str) -> str:
    """A collision-proof object key that keeps the original extension.

    Prefixed by owner so a bucket listing is navigable, and suffixed with a UUID
    so two people uploading `lecture.m4a` cannot overwrite each other.
    """
    suffix = ""
    if "." in filename:
        candidate = filename.rsplit(".", 1)[-1]
        # Guard against a "filename" that is really a path or an absurd extension.
        if candidate.isalnum() and len(candidate) <= 10:
            suffix = f".{candidate.lower()}"

    return f"uploads/{owner_id}/{uuid.uuid4()}{suffix}"


def presign_upload(*, key: str, content_type: str) -> str:
    """A URL the client may PUT the file to, valid briefly."""
    try:
        url: str = _client().generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.s3_bucket, "Key": key, "ContentType": content_type},
            ExpiresIn=settings.s3_presign_ttl_seconds,
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network failure
        raise StorageError(f"Could not prepare the upload: {exc}") from exc
    return url


def presign_download(*, key: str) -> str:
    """A short-lived URL for reading an object."""
    try:
        url: str = _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": settings.s3_bucket, "Key": key},
            ExpiresIn=settings.s3_presign_ttl_seconds,
        )
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network failure
        raise StorageError(f"Could not prepare the download: {exc}") from exc
    return url


def download_bytes(*, key: str) -> bytes:
    """Read an object into memory.

    Used by the note worker, which must hand the audio to Gemini. Fine for
    lecture recordings; anything much larger should be streamed instead.
    """
    try:
        response = _client().get_object(Bucket=settings.s3_bucket, Key=key)
        data: bytes = response["Body"].read()
    except (BotoCoreError, ClientError) as exc:
        raise StorageError(f"Could not read the file: {exc}") from exc
    return data


def delete_object(*, key: str) -> None:
    """Remove an object. Silent when it is already gone.

    Deleting a database row does **not** delete the object — storage knows
    nothing about foreign keys, so every cascade must call this explicitly or
    leak paid-for bytes.
    """
    try:
        _client().delete_object(Bucket=settings.s3_bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:  # pragma: no cover - network failure
        raise StorageError(f"Could not delete the file: {exc}") from exc
