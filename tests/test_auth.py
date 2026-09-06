"""End-to-end tests for the auth flow."""

from __future__ import annotations

from httpx import AsyncClient

API = "/api/v1"


async def register(client: AsyncClient, payload: dict[str, str]) -> None:
    response = await client.post(f"{API}/auth/register", json=payload)
    assert response.status_code == 201, response.text


async def login(client: AsyncClient, payload: dict[str, str]) -> dict[str, str]:
    response = await client.post(
        f"{API}/auth/login",
        json={"email": payload["email"], "password": payload["password"]},
    )
    assert response.status_code == 200, response.text
    return response.json()


class TestRegistration:
    async def test_creates_account_without_leaking_the_hash(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        response = await client.post(f"{API}/auth/register", json=user_payload)

        assert response.status_code == 201
        body = response.json()
        assert body["email"] == user_payload["email"]
        assert body["is_active"] is True
        assert "password" not in body
        assert "password_hash" not in body

    async def test_duplicate_email_conflicts(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        response = await client.post(f"{API}/auth/register", json=user_payload)

        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    async def test_email_is_normalised_to_lowercase(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, {**user_payload, "email": "ADA@Example.edu"})

        # The lowercase form must now collide.
        response = await client.post(f"{API}/auth/register", json=user_payload)
        assert response.status_code == 409

    async def test_short_password_rejected(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/auth/register", json={**user_payload, "password": "short"}
        )

        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestLogin:
    async def test_returns_a_token_pair(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)

        assert tokens["token_type"] == "bearer"
        assert tokens["access_token"]
        assert tokens["refresh_token"]
        assert tokens["expires_in"] > 0

    async def test_wrong_password_is_rejected(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        response = await client.post(
            f"{API}/auth/login",
            json={"email": user_payload["email"], "password": "not-the-password"},
        )

        assert response.status_code == 401

    async def test_unknown_email_gives_the_same_error_as_a_wrong_password(
        self, client: AsyncClient
    ) -> None:
        """The response must not reveal whether an account exists."""
        response = await client.post(
            f"{API}/auth/login",
            json={"email": "nobody@example.edu", "password": "whatever-password"},
        )

        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password"


class TestCurrentUser:
    async def test_me_requires_a_token(self, client: AsyncClient) -> None:
        response = await client.get(f"{API}/users/me")
        assert response.status_code == 401

    async def test_me_returns_the_profile(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)

        response = await client.get(
            f"{API}/users/me",
            headers={"Authorization": f"Bearer {tokens['access_token']}"},
        )

        assert response.status_code == 200
        assert response.json()["email"] == user_payload["email"]

    async def test_refresh_token_is_not_accepted_as_an_access_token(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)

        response = await client.get(
            f"{API}/users/me",
            headers={"Authorization": f"Bearer {tokens['refresh_token']}"},
        )

        assert response.status_code == 401

    async def test_patch_updates_only_supplied_fields(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        response = await client.patch(
            f"{API}/users/me", json={"full_name": "Ada Byron"}, headers=headers
        )

        assert response.status_code == 200
        assert response.json()["full_name"] == "Ada Byron"
        assert response.json()["institute"] == user_payload["institute"]


class TestRefreshRotation:
    async def test_refresh_returns_a_new_pair(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)

        response = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )

        assert response.status_code == 200
        assert response.json()["refresh_token"] != tokens["refresh_token"]

    async def test_reusing_a_rotated_token_revokes_the_whole_family(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        """Replay of an already-rotated token means it was stolen."""
        await register(client, user_payload)
        tokens = await login(client, user_payload)

        rotated = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        new_refresh = rotated.json()["refresh_token"]

        # Replay the old one.
        replay = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert replay.status_code == 401

        # The successor issued moments ago must now be dead too.
        followup = await client.post(f"{API}/auth/refresh", json={"refresh_token": new_refresh})
        assert followup.status_code == 401

    async def test_garbage_token_is_rejected(self, client: AsyncClient) -> None:
        response = await client.post(f"{API}/auth/refresh", json={"refresh_token": "nonsense"})
        assert response.status_code == 401


class TestLogout:
    async def test_logout_revokes_the_supplied_token(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        tokens = await login(client, user_payload)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        response = await client.post(
            f"{API}/auth/logout",
            json={"refresh_token": tokens["refresh_token"]},
            headers=headers,
        )
        assert response.status_code == 204

        reuse = await client.post(
            f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert reuse.status_code == 401

    async def test_logout_without_a_token_revokes_every_session(
        self, client: AsyncClient, user_payload: dict[str, str]
    ) -> None:
        await register(client, user_payload)
        first = await login(client, user_payload)
        second = await login(client, user_payload)

        response = await client.post(
            f"{API}/auth/logout",
            json={},
            headers={"Authorization": f"Bearer {second['access_token']}"},
        )
        assert response.status_code == 204

        for tokens in (first, second):
            reuse = await client.post(
                f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
            )
            assert reuse.status_code == 401


class TestHealth:
    async def test_liveness(self, client: AsyncClient) -> None:
        response = await client.get(f"{API}/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_readiness_checks_the_database(self, client: AsyncClient) -> None:
        response = await client.get(f"{API}/health/ready")
        assert response.status_code == 200
        assert response.json()["database"] == "ok"
