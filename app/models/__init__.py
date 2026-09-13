"""SQLAlchemy models.

Every model must be imported here so that Alembic autogenerate and
``Base.metadata`` see the full schema.
"""

from app.models.classroom import Classroom, ClassroomMember, ClassroomType, MemberRole
from app.models.note import Asset, Note, NoteSourceType, NoteStatus
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "Asset",
    "Classroom",
    "ClassroomMember",
    "ClassroomType",
    "MemberRole",
    "Note",
    "NoteSourceType",
    "NoteStatus",
    "RefreshToken",
    "User",
]
