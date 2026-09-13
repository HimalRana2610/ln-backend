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


class ValidationFailedError(AppError):
    """A well-formed request the rules refuse, with a specific reason code."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "invalid_request"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Try again shortly."

    def __init__(self, message: str | None = None, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "This feature is not available on this server"


async def app_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    headers: dict[str, str] | None = None
    if isinstance(exc, AuthenticationError):
        headers = {"WWW-Authenticate": "Bearer"}
    elif isinstance(exc, RateLimitedError) and exc.retry_after is not None:
        headers = {"Retry-After": str(exc.retry_after)}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
        headers=headers,
    )
