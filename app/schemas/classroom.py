"""Classroom request/response bodies."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.classroom import CLASS_CODE_LENGTH, ClassroomType, MemberRole

# Gradient pairs offered by the UI, carried over from the old app's class cards.
THEME_COLORS = [
    "from-blue-500 to-indigo-600",
    "from-blue-500 to-sky-500",
    "from-blue-600 to-cyan-600",
    "from-green-500 to-emerald-600",
    "from-rose-500 to-pink-600",
    "from-amber-500 to-orange-600",
    "from-violet-500 to-purple-600",
    "from-slate-600 to-slate-800",
]


class ClassroomCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=200)
    section: str | None = Field(default=None, max_length=120)
    type: ClassroomType = ClassroomType.PERSONAL
    theme_color: str | None = Field(default=None, max_length=60)

    @field_validator("theme_color")
    @classmethod
    def _known_theme(cls, value: str | None) -> str | None:
        if value is not None and value not in THEME_COLORS:
            raise ValueError("Unknown theme colour")
        return value


class ClassroomUpdate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    name: str | None = Field(default=None, min_length=1, max_length=200)
    section: str | None = Field(default=None, max_length=120)
    type: ClassroomType | None = None
    theme_color: str | None = Field(default=None, max_length=60)

    @field_validator("theme_color")
    @classmethod
    def _known_theme(cls, value: str | None) -> str | None:
        if value is not None and value not in THEME_COLORS:
            raise ValueError("Unknown theme colour")
        return value


class JoinClassroomRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    code: str = Field(min_length=CLASS_CODE_LENGTH, max_length=CLASS_CODE_LENGTH)

    @field_validator("code")
    @classmethod
    def _normalise(cls, value: str) -> str:
        """Codes are stored uppercase; accept whatever case the user typed."""
        return value.upper()


class MemberRoleUpdate(BaseModel):
    role: MemberRole

    @field_validator("role")
    @classmethod
    def _not_owner(cls, value: MemberRole) -> MemberRole:
        # Ownership transfer is a separate, deliberate operation.
        if value is MemberRole.OWNER:
            raise ValueError("Use ownership transfer to make someone the owner")
        return value


class MemberRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    full_name: str
    role: MemberRole
    joined_at: datetime


class ClassroomRead(BaseModel):
    """A classroom as seen by one member.

    ``my_role`` and ``member_count`` are per-viewer, which is why this is
    assembled by the service rather than read straight off the ORM object.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    section: str | None
    code: str
    type: ClassroomType
    theme_color: str
    owner_id: uuid.UUID
    owner_name: str
    my_role: MemberRole
    member_count: int
    created_at: datetime
