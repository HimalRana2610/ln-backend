"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.exceptions import AppError, app_error_handler
from app.db.session import engine


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    yield
    # Close pooled connections so the process exits cleanly on SIGTERM.
    await engine.dispose()


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Render 422s in the same envelope as every other error."""
    assert isinstance(exc, RequestValidationError)
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={
            "error": {
                "code": "validation_error",
                "message": "Request payload failed validation",
                "details": [
                    {"field": ".".join(str(p) for p in err["loc"][1:]), "message": err["msg"]}
                    for err in exc.errors()
                ],
            }
        },
    )


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.project_name,
        version="0.1.0",
        lifespan=lifespan,
        # Interactive docs are useful in development and noise in production.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None if settings.is_production else "/redoc",
        openapi_url=None if settings.is_production else "/openapi.json",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)

    app.include_router(api_router, prefix=settings.api_v1_prefix)
    return app


app = create_app()
