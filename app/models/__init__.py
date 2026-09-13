"""SQLAlchemy models.

Every model must be imported here so that Alembic autogenerate and
``Base.metadata`` see the full schema.
"""

from app.models.attendance import (
    AttendanceRecord,
    AttendanceSession,
    AttendanceVerification,
    RecordStatus,
    SessionStatus,
)
from app.models.classroom import Classroom, ClassroomMember, ClassroomType, MemberRole
from app.models.note import Asset, Note, NoteSourceType, NoteStatus
from app.models.notification import PushPlatform, PushToken
from app.models.post import ClassroomPost, PostKind, Submission
from app.models.quiz import QuizAnswer, QuizQuestion, QuizStatus
from app.models.refresh_token import RefreshToken
from app.models.security import (
    AlertSeverity,
    AlertType,
    DevicePlatform,
    FaceEnrollment,
    OtpCode,
    OtpPurpose,
    SecurityAlert,
    StudentBlock,
    UserDevice,
)
from app.models.user import User

__all__ = [
    "AlertSeverity",
    "AlertType",
    "Asset",
    "AttendanceRecord",
    "AttendanceSession",
    "AttendanceVerification",
    "Classroom",
    "ClassroomMember",
    "ClassroomPost",
    "ClassroomType",
    "DevicePlatform",
    "FaceEnrollment",
    "MemberRole",
    "Note",
    "NoteSourceType",
    "NoteStatus",
    "OtpCode",
    "OtpPurpose",
    "PostKind",
    "PushPlatform",
    "PushToken",
    "QuizAnswer",
    "QuizQuestion",
    "QuizStatus",
    "RecordStatus",
    "RefreshToken",
    "SecurityAlert",
    "SessionStatus",
    "StudentBlock",
    "Submission",
    "User",
    "UserDevice",
]
