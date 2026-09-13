"""Aggregates every v1 route module into one router.

New feature routers are registered here and nowhere else.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import auth, classrooms, health, notes, users

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(classrooms.router)
api_router.include_router(notes.router)
