"""End-to-end tests for classrooms and membership.

The permission rules are the point of these tests: who may edit, who may remove
whom, and what a non-member is allowed to learn about a classroom's existence.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

API = "/api/v1"


async def signup(client: AsyncClient, email: str, name: str) -> dict[str, str]:
    """Register a user and return an Authorization header for them."""
    password = "correct-horse-battery"
    response = await client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": name, "password": password},
    )
    assert response.status_code == 201, response.text

    tokens = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    assert tokens.status_code == 200, tokens.text
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}


@pytest.fixture
async def teacher(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "teacher@example.edu", "Grace Hopper")


@pytest.fixture
async def student(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "student@example.edu", "Alan Turing")


async def make_classroom(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    payload = {"name": "Discrete Mathematics", "section": "B", **overrides}
    response = await client.post(f"{API}/classrooms", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


class TestCreate:
    async def test_creator_becomes_owner_and_first_member(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        body = await make_classroom(client, teacher)

        assert body["name"] == "Discrete Mathematics"
        assert body["my_role"] == "owner"
        assert body["member_count"] == 1
        assert body["owner_name"] == "Grace Hopper"

    async def test_join_code_is_six_uppercase_characters(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        body = await make_classroom(client, teacher)

        assert len(body["code"]) == 6
        assert body["code"].isalnum()
        assert body["code"] == body["code"].upper()

    async def test_codes_are_unique_across_classrooms(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        codes = {
            (await make_classroom(client, teacher, name=f"Class {i}"))["code"]
            for i in range(8)
        }
        assert len(codes) == 8

    async def test_defaults_to_personal_type(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        body = await make_classroom(client, teacher)
        assert body["type"] == "personal"

    async def test_rejects_an_unknown_theme_colour(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms",
            json={"name": "Physics", "theme_color": "from-puce-500 to-beige-600"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post(f"{API}/classrooms", json={"name": "Physics"})
        assert response.status_code == 401


class TestJoin:
    async def test_student_joins_by_code(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)

        response = await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        assert response.status_code == 200
        assert response.json()["my_role"] == "student"
        assert response.json()["member_count"] == 2

    async def test_code_is_case_insensitive(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)

        response = await client.post(
            f"{API}/classrooms/join",
            json={"code": classroom["code"].lower()},
            headers=student,
        )
        assert response.status_code == 200

    async def test_unknown_code_is_not_found(
        self, client: AsyncClient, student: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/join", json={"code": "ZZZZZZ"}, headers=student
        )
        assert response.status_code == 404

    async def test_joining_twice_conflicts(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        body = {"code": classroom["code"]}

        await client.post(f"{API}/classrooms/join", json=body, headers=student)
        second = await client.post(f"{API}/classrooms/join", json=body, headers=student)

        assert second.status_code == 409

    async def test_owner_cannot_join_their_own_classroom(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)

        response = await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=teacher
        )
        assert response.status_code == 409


class TestVisibility:
    async def test_list_returns_only_my_classrooms(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        await make_classroom(client, teacher, name="Mine")

        mine = await client.get(f"{API}/classrooms", headers=teacher)
        theirs = await client.get(f"{API}/classrooms", headers=student)

        assert [c["name"] for c in mine.json()] == ["Mine"]
        assert theirs.json() == []

    async def test_non_member_gets_404_not_403(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        """A stranger must not be able to confirm a classroom id exists."""
        classroom = await make_classroom(client, teacher)

        response = await client.get(f"{API}/classrooms/{classroom['id']}", headers=student)

        assert response.status_code == 404

    async def test_members_list_visible_to_members(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        response = await client.get(
            f"{API}/classrooms/{classroom['id']}/members", headers=student
        )

        assert response.status_code == 200
        roles = {m["email"]: m["role"] for m in response.json()}
        assert roles == {
            "teacher@example.edu": "owner",
            "student@example.edu": "student",
        }


class TestEditing:
    async def test_owner_can_rename(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)

        response = await client.patch(
            f"{API}/classrooms/{classroom['id']}",
            json={"name": "Renamed"},
            headers=teacher,
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"
        # Omitted fields are untouched.
        assert response.json()["section"] == "B"

    async def test_student_cannot_edit(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        response = await client.patch(
            f"{API}/classrooms/{classroom['id']}", json={"name": "Hax"}, headers=student
        )

        assert response.status_code == 403

    async def test_student_cannot_delete(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        response = await client.delete(
            f"{API}/classrooms/{classroom['id']}", headers=student
        )
        assert response.status_code == 403

    async def test_owner_deletes_and_it_disappears_for_members(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        deleted = await client.delete(
            f"{API}/classrooms/{classroom['id']}", headers=teacher
        )
        assert deleted.status_code == 204

        # Membership rows cascade, so it vanishes from the student's list too.
        remaining = await client.get(f"{API}/classrooms", headers=student)
        assert remaining.json() == []


class TestMembership:
    async def test_student_can_leave(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )

        left = await client.post(
            f"{API}/classrooms/{classroom['id']}/leave", headers=student
        )
        assert left.status_code == 204

        assert (await client.get(f"{API}/classrooms", headers=student)).json() == []

    async def test_owner_cannot_leave(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)

        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/leave", headers=teacher
        )

        assert response.status_code == 409
        assert "owner" in response.json()["error"]["message"].lower()

    async def test_owner_promotes_a_student_to_teacher(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        joined = await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        assert joined.status_code == 200

        members = await client.get(
            f"{API}/classrooms/{classroom['id']}/members", headers=teacher
        )
        student_id = next(
            m["user_id"] for m in members.json() if m["role"] == "student"
        )

        promoted = await client.patch(
            f"{API}/classrooms/{classroom['id']}/members/{student_id}",
            json={"role": "teacher"},
            headers=teacher,
        )

        assert promoted.status_code == 200
        assert promoted.json()["role"] == "teacher"

        # And the promotion is real: they can now edit.
        edit = await client.patch(
            f"{API}/classrooms/{classroom['id']}",
            json={"name": "Co-taught"},
            headers=student,
        )
        assert edit.status_code == 200

    async def test_cannot_promote_someone_to_owner(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        members = await client.get(
            f"{API}/classrooms/{classroom['id']}/members", headers=teacher
        )
        student_id = next(m["user_id"] for m in members.json() if m["role"] == "student")

        response = await client.patch(
            f"{API}/classrooms/{classroom['id']}/members/{student_id}",
            json={"role": "owner"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_teacher_removes_a_student(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        members = await client.get(
            f"{API}/classrooms/{classroom['id']}/members", headers=teacher
        )
        student_id = next(m["user_id"] for m in members.json() if m["role"] == "student")

        removed = await client.delete(
            f"{API}/classrooms/{classroom['id']}/members/{student_id}", headers=teacher
        )
        assert removed.status_code == 204

        assert (await client.get(f"{API}/classrooms", headers=student)).json() == []

    async def test_student_cannot_remove_the_owner(
        self, client: AsyncClient, teacher: dict[str, str], student: dict[str, str]
    ) -> None:
        classroom = await make_classroom(client, teacher)
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        members = await client.get(
            f"{API}/classrooms/{classroom['id']}/members", headers=student
        )
        owner_id = next(m["user_id"] for m in members.json() if m["role"] == "owner")

        response = await client.delete(
            f"{API}/classrooms/{classroom['id']}/members/{owner_id}", headers=student
        )
        assert response.status_code == 403


class TestThemeColors:
    async def test_palette_is_served_to_clients(self, client: AsyncClient) -> None:
        response = await client.get(f"{API}/classrooms/theme-colors")

        assert response.status_code == 200
        colors = response.json()
        assert len(colors) >= 4
        assert all(c.startswith("from-") and " to-" in c for c in colors)
