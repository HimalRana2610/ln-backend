"""User-facing representations.

``UserRead`` is the only shape ever returned to a client - it has no
``password_hash`` field, so a hash cannot leak through a response by accident.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    full_name: str
    institute: str | None
    is_active: bool
    is_email_verified: bool
    created_at: datetime


class UserUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    institute: str | None = Field(default=None, max_length=200)
