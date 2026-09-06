"""Settings behaviour that only bites in deployment.

These are pure-function tests over `Settings`, so they need no database. They
exist because the failure they guard against — a hosted Postgres URL that
asyncpg refuses — appears for the first time in production, where it is most
expensive to discover.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import Settings

BASE = {"secret_key": "x" * 40, "environment": "test"}

# The shape Neon and Supabase actually hand you.
NEON_URL = (
    "postgresql://user:pw@ep-cool-dawn-123.eu-central-1.aws.neon.tech/lecturenote"
    "?sslmode=require&channel_binding=require"
)
LOCAL_URL = "postgresql://lecturenote:lecturenote@localhost:5432/lecturenote"


def make(**overrides: object) -> Settings:
    return Settings(**{**BASE, **overrides})  # type: ignore[arg-type]


class TestUrlCleaning:
    def test_strips_libpq_only_parameters(self) -> None:
        """asyncpg raises TypeError on sslmode/channel_binding.

        They are libpq options. Leaving them in the URL is the single most
        common cause of a backend that works locally and dies on first deploy.
        """
        settings = make(database_url=NEON_URL)
        query = parse_qs(urlsplit(settings.sqlalchemy_url).query)

        assert "sslmode" not in query
        assert "channel_binding" not in query

    def test_keeps_host_and_database(self) -> None:
        settings = make(database_url=NEON_URL)
        parts = urlsplit(settings.sqlalchemy_url)

        assert parts.hostname == "ep-cool-dawn-123.eu-central-1.aws.neon.tech"
        assert parts.path == "/lecturenote"

    def test_selects_the_asyncpg_driver(self) -> None:
        assert make(database_url=LOCAL_URL).sqlalchemy_url.startswith(
            "postgresql+asyncpg://"
        )


class TestSsl:
    def test_hosted_url_requests_tls(self) -> None:
        settings = make(database_url=NEON_URL)

        assert settings.db_requires_ssl is True
        assert settings.engine_connect_args["ssl"] is True

    def test_local_url_does_not(self) -> None:
        settings = make(database_url=LOCAL_URL)

        assert settings.db_requires_ssl is False
        assert "ssl" not in settings.engine_connect_args

    @pytest.mark.parametrize("mode", ["disable", "allow"])
    def test_explicit_opt_out_is_respected(self, mode: str) -> None:
        settings = make(database_url=f"{LOCAL_URL}?sslmode={mode}")
        assert settings.db_requires_ssl is False


class TestServerless:
    def test_disables_prepared_statement_caching(self) -> None:
        """Behind a transaction pooler, a cached prepared statement can vanish.

        The server connection is handed to another client between statements,
        which surfaces as "prepared statement _asyncpg_stmt_x does not exist"
        under load and nowhere else.
        """
        settings = make(database_url=NEON_URL, db_serverless=True)
        query = parse_qs(urlsplit(settings.sqlalchemy_url).query)

        assert query["prepared_statement_cache_size"] == ["0"]
        assert settings.engine_connect_args["statement_cache_size"] == 0

    def test_long_running_deployment_keeps_caching(self) -> None:
        settings = make(database_url=NEON_URL, db_serverless=False)
        query = parse_qs(urlsplit(settings.sqlalchemy_url).query)

        assert "prepared_statement_cache_size" not in query
        assert "statement_cache_size" not in settings.engine_connect_args


class TestCorsOrigins:
    def test_accepts_a_comma_separated_string(self) -> None:
        settings = make(
            database_url=LOCAL_URL,
            cors_origins="https://ln-web.vercel.app, http://localhost:3000",
        )
        assert settings.cors_origins == [
            "https://ln-web.vercel.app",
            "http://localhost:3000",
        ]

    def test_empty_means_no_origins(self) -> None:
        assert make(database_url=LOCAL_URL, cors_origins="").cors_origins == []
