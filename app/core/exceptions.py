"""Domain exceptions and the handlers that turn them into HTTP responses.

Services raise these; they know nothing about HTTP. Only ``app.main`` maps them
to status codes, so the same service is reusable from a CLI or a worker.
"""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    status_code: int = status.HTTP_400_BAD_REQUEST
    code: str = "app_error"
    message: str = "Something went wrong"

    def __init__(self, message: str | None = None) -> None:
        if message:
            self.message = message
        super().__init__(self.message)


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"
    message = "Resource not found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "Resource already exists"


class AuthenticationError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "authentication_failed"
    message = "Could not validate credentials"


class PermissionDeniedError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "permission_denied"
    message = "You do not have access to this resource"


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers = (
        {"WWW-Authenticate": "Bearer"} if isinstance(exc, AuthenticationError) else None
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )
