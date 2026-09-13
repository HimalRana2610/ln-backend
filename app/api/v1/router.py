"""Aggregates every v1 route module into one router.

New feature routers are registered here and nowhere else.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.routes import (
    attendance,
    auth,
    classrooms,
    health,
    me,
    notes,
    posts,
    quiz,
    security,
    users,
)

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(users.router)
api_router.include_router(classrooms.router)
api_router.include_router(notes.router)
api_router.include_router(posts.router)
api_router.include_router(attendance.router)
api_router.include_router(security.router)
api_router.include_router(me.router)
api_router.include_router(quiz.router)
