"""Bodies for the signed-in user's own resources: to-do, push tokens, account."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.notification import PushPlatform


class ToDoStatus(enum.StrEnum):
    ASSIGNED = "assigned"
    MISSING = "missing"
    DONE = "done"


class ToDoItem(BaseModel):
    post_id: uuid.UUID
    classroom_id: uuid.UUID
    classroom_name: str
    title: str
    description: str | None
    due_date: datetime | None
    author_name: str
    created_at: datetime
    submitted_at: datetime | None
    is_late: bool
    status: ToDoStatus


class PushTokenRegister(BaseModel):
    token: str = Field(min_length=1, max_length=512)
    platform: PushPlatform


class PushTokenRemove(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class AccountDelete(BaseModel):
    # Re-entering the password stops a borrowed, unlocked session from erasing
    # someone's account and every file they own.
    password: str = Field(min_length=1, max_length=256)
