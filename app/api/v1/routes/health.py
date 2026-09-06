"""Liveness and readiness probes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok", "environment": settings.environment}


@router.get("/health/ready")
async def readiness(db: DbSession) -> JSONResponse:
    """Readiness: dependencies are reachable, so it is safe to route traffic here."""
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unavailable", "database": str(exc)},
        )
    return JSONResponse(content={"status": "ready", "database": "ok"})
