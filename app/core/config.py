"""Application settings.

Every value is read from the environment, so the same image runs in dev, CI and
production with nothing but env vars changing. Import the shared ``settings``
singleton rather than constructing ``Settings`` yourself.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# libpq understands these; asyncpg does not, and raises TypeError on connect.
# Managed providers (Neon, Supabase) put them in the URL they hand you, so they
# are stripped and re-expressed through connect_args.
_LIBPQ_ONLY_PARAMS = frozenset(
    {"sslmode", "channel_binding", "sslrootcert", "sslcert", "sslkey", "target_session_attrs"}
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ------------------------------------------------------
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    project_name: str = "LectureNote AI API"
    api_v1_prefix: str = "/api/v1"

    # --- Security ---------------------------------------------------------
    # Generate with: python -c "import secrets; print(secrets.token_urlsafe(64))"
    secret_key: str = Field(min_length=32)
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 30

    # Comma-separated list of origins allowed to call the API. NoDecode stops
    # pydantic-settings JSON-parsing it, so the validator below can accept
    # either a plain comma-separated string or a JSON array.
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Database ---------------------------------------------------------
    database_url: PostgresDsn
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    # True when deployed to a serverless platform (Vercel functions). Each
    # invocation is short-lived and connects through a transaction-mode pooler,
    # which needs a different pooling and prepared-statement strategy entirely.
    db_serverless: bool = False

    # --- AI ---------------------------------------------------------------
    # https://aistudio.google.com/apikey. The backup key takes over when the
    # primary one is rate limited, which a single free-tier key reliably is.
    gemini_api_key: str = ""
    gemini_api_key_backup: str = ""

    # Google retires model names on a schedule, and closes older ones to *new*
    # API keys well before deleting them — a retired model still appears in
    # `models.list()` but returns 404 on the first real call. That failure looks
    # like a broken key, so this is configurable: when it happens, change the
    # env var rather than shipping a patch.
    gemini_model: str = "gemini-3.7-flash"

    # YouTube transcripts.
    supadata_api_key: str = ""
    supadata_api_key_backup: str = ""

    # --- Note generation --------------------------------------------------
    # Whether this process should pick up pending notes itself. True for a
    # long-running server; set false on a serverless host, where a request's
    # background work is killed the moment the response is sent, and run
    # `python -m app.worker` somewhere that permits long jobs instead.
    notes_inline_worker: bool = True

    # A note stuck in `processing` for longer than this is assumed dead and is
    # retried - a worker can be killed mid-job at any time.
    notes_stale_after_minutes: int = 15

    # --- Object storage (S3 compatible: MinIO locally, R2/S3 in prod) -----
    s3_endpoint_url: str | None = None
    s3_region: str = "auto"
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket: str = "lecture-note"
    s3_presign_ttl_seconds: int = 900

    # Total bytes of files one classroom may hold. Supabase's free tier is 1 GB
    # for the whole project, and lecture slides eat it far faster than notes;
    # without a cap the first sign of trouble is every upload failing at once.
    classroom_storage_limit_mb: int = 300

    # --- Email (OTP) ------------------------------------------------------
    # Gmail: smtp.gmail.com:587 with an App Password, not the account password.
    # Unset outside production, codes are written to the log instead so local
    # development needs no mail account.
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_email: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "LectureNote AI"

    otp_ttl_minutes: int = 10
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_max_per_hour: int = 5

    # --- Signed attendance ------------------------------------------------
    # How far a signed request's timestamp may be from the server's clock.
    signature_max_skew_seconds: int = 120

    # --- Face recognition -------------------------------------------------
    # An HTTP service that turns one face photo into an embedding: multipart
    # field `file`, response `{"embedding": [floats]}` — the shape the old app's
    # InsightFace service used. Unset, face enrolment reports itself
    # unavailable. Photos are sent to it and discarded; only vectors are kept.
    face_embedding_url: str = ""
    face_embedding_api_key: str = ""
    face_model_version: str = "insightface"
    face_match_threshold: float = 0.5

    # --- Push notifications (Firebase Cloud Messaging) ---------------------
    # A service-account key: either the JSON itself or a path to the file.
    # Unset, pushes are written to the log instead of sent.
    fcm_service_account_json: str = ""
    # Defaults to the key's own `project_id`.
    fcm_project_id: str = ""

    # --- Live quizzes -----------------------------------------------------
    # How often clients poll an open quiz. The API has no push channel for
    # live updates on a serverless host, so this is a documented trade rather
    # than an accident: returned to clients so both use the same value.
    quiz_poll_interval_seconds: int = 3

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accept either a JSON list or a plain comma-separated string."""
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def db_requires_ssl(self) -> bool:
        """Whether the provider asked for TLS via ``sslmode``.

        Every hosted Postgres does; a local Docker container does not.
        """
        query = dict(parse_qsl(urlsplit(str(self.database_url)).query))
        return query.get("sslmode", "").lower() not in {"", "disable", "allow"}

    @property
    def sqlalchemy_url(self) -> str:
        """asyncpg-flavoured DSN, with libpq-only query parameters removed."""
        parts = urlsplit(str(self.database_url))
        kept = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _LIBPQ_ONLY_PARAMS
        ]

        if self.db_serverless:
            # SQLAlchemy's asyncpg dialect reads this from the URL. Behind a
            # transaction pooler a server connection is handed to a different
            # client between statements, so a cached prepared statement may not
            # exist any more - "prepared statement does not exist" at random.
            kept.append(("prepared_statement_cache_size", "0"))

        cleaned = urlunsplit(parts._replace(query=urlencode(kept)))
        return cleaned.replace("postgresql://", "postgresql+asyncpg://", 1)

    @property
    def engine_connect_args(self) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if self.db_requires_ssl:
            args["ssl"] = True
        if self.db_serverless:
            # The asyncpg-level twin of prepared_statement_cache_size above.
            args["statement_cache_size"] = 0
        return args


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


settings = get_settings()
