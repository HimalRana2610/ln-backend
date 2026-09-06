"""SQLAlchemy models.

Every model must be imported here so that Alembic autogenerate and
``Base.metadata`` see the full schema.
"""

from app.models.classroom import Classroom, ClassroomMember, ClassroomType, MemberRole
from app.models.refresh_token import RefreshToken
from app.models.user import User

__all__ = [
    "Classroom",
    "ClassroomMember",
    "ClassroomType",
    "MemberRole",
    "RefreshToken",
    "User",
]
