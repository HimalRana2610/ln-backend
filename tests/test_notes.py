"""End-to-end tests for notes and uploads.

Gemini and S3 are stubbed. The point of these tests is the *state machine* and
the *permissions* — that a note is created pending, becomes ready or failed and
never hangs, and that only the right people can read or change it. Whether
Gemini writes good prose is not something a test can assert.
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

from app.services import ai_service, note_service, storage_service

API = "/api/v1"


async def signup(client: AsyncClient, email: str, name: str) -> dict[str, str]:
    password = "correct-horse-battery"
    response = await client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": name, "password": password},
    )
    assert response.status_code == 201, response.text

    tokens = await client.post(
        f"{API}/auth/login", json={"email": email, "password": password}
    )
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}


@pytest.fixture
async def teacher(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "teacher@example.edu", "Grace Hopper")


@pytest.fixture
async def student(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "student@example.edu", "Alan Turing")


@pytest.fixture
async def outsider(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "outsider@example.edu", "Ada Lovelace")


@pytest.fixture
async def classroom(client: AsyncClient, teacher: dict[str, str]) -> dict[str, Any]:
    response = await client.post(
        f"{API}/classrooms", json={"name": "Discrete Mathematics"}, headers=teacher
    )
    return response.json()


@pytest.fixture
def fake_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    """A Gemini that always succeeds, returning a recognisable note."""

    async def _generate(*_args: object, **_kwargs: object) -> ai_service.GeneratedNote:
        return ai_service.GeneratedNote(
            title="Graph Theory",
            markdown="# Graph Theory\n\n## Definitions\n\nA graph is a set of vertices.",
        )

    for name in ("generate_from_text", "generate_from_audio", "generate_from_pdf"):
        monkeypatch.setattr(note_service.ai_service, name, _generate)


@pytest.fixture
def fake_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Storage that signs URLs and returns bytes without touching S3."""
    monkeypatch.setattr(
        note_service.storage_service,
        "presign_upload",
        lambda **kwargs: f"https://storage.test/{kwargs['key']}?signed=1",
    )
    monkeypatch.setattr(
        note_service.storage_service, "download_bytes", lambda **_: b"fake-audio-bytes"
    )
    monkeypatch.setattr(note_service.storage_service, "delete_object", lambda **_: None)


async def create_note(
    client: AsyncClient, classroom_id: str, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    payload = {"text": "Today we covered graphs and their properties.", **body}
    response = await client.post(
        f"{API}/classrooms/{classroom_id}/notes", json=payload, headers=headers
    )
    assert response.status_code == 202, response.text
    return response.json()


class TestCreation:
    async def test_returns_immediately_as_pending(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        """Generation takes minutes; the request must not wait for it."""
        note = await create_note(client, classroom["id"], teacher)

        assert note["status"] == "pending"
        assert note["markdown"] == ""
        assert note["source_type"] == "text"

    async def test_defaults_the_date_to_today(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)
        assert note["date"]

    async def test_accepts_an_explicit_lecture_date(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        # A student may write up a recording days later.
        note = await create_note(client, classroom["id"], teacher, date="2026-03-01")
        assert note["date"] == "2026-03-01"

    async def test_requires_exactly_one_source(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        none_given = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes", json={}, headers=teacher
        )
        assert none_given.status_code == 422

        two_given = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"text": "hello", "youtube_url": "https://youtu.be/dQw4w9WgXcQ"},
            headers=teacher,
        )
        assert two_given.status_code == 422

    async def test_rejects_a_non_youtube_link(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"youtube_url": "https://vimeo.com/12345"},
            headers=teacher,
        )
        assert response.status_code == 409

    async def test_accepts_youtube_url_forms(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        for url in (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ):
            response = await client.post(
                f"{API}/classrooms/{classroom['id']}/notes",
                json={"youtube_url": url},
                headers=teacher,
            )
            assert response.status_code == 202, url

    async def test_non_member_cannot_create(
        self, client: AsyncClient, classroom: dict[str, Any], outsider: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"text": "hello"},
            headers=outsider,
        )
        assert response.status_code == 404


class TestGeneration:
    async def test_processing_fills_in_the_note(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
        fake_gemini: None,
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)

        await note_service.NoteService(db_session).process(note["id"])

        fetched = await client.get(f"{API}/notes/{note['id']}", headers=teacher)
        body = fetched.json()

        assert body["status"] == "ready"
        assert body["title"] == "Graph Theory"
        assert "## Definitions" in body["markdown"]
        assert body["error_message"] is None

    async def test_the_title_comes_from_the_generated_heading(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
        fake_gemini: None,
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)
        assert note["title"] == "Generating notes…"

        await note_service.NoteService(db_session).process(note["id"])
        fetched = await client.get(f"{API}/notes/{note['id']}", headers=teacher)

        assert fetched.json()["title"] == "Graph Theory"

    async def test_failure_is_recorded_not_raised(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A note must never hang in `processing` because the model errored."""

        async def _boom(*_args: object, **_kwargs: object) -> ai_service.GeneratedNote:
            raise ai_service.AIError("model is on fire")

        monkeypatch.setattr(note_service.ai_service, "generate_from_text", _boom)

        note = await create_note(client, classroom["id"], teacher)
        await note_service.NoteService(db_session).process(note["id"])

        fetched = await client.get(f"{API}/notes/{note['id']}", headers=teacher)
        body = fetched.json()

        assert body["status"] == "failed"
        assert "model is on fire" in body["error_message"]

    async def test_missing_api_key_is_a_clear_failure(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(ai_service.settings, "gemini_api_key", "")
        monkeypatch.setattr(ai_service.settings, "gemini_api_key_backup", "")

        note = await create_note(client, classroom["id"], teacher)
        await note_service.NoteService(db_session).process(note["id"])

        fetched = await client.get(f"{API}/notes/{note['id']}", headers=teacher)
        assert fetched.json()["status"] == "failed"
        assert "GEMINI_API_KEY" in fetched.json()["error_message"]

    async def test_claiming_marks_the_note_processing(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)
        service = note_service.NoteService(db_session)

        claimed = await service.claim_next_pending()
        assert str(claimed) == note["id"]

        fetched = await client.get(f"{API}/notes/{note['id']}", headers=teacher)
        assert fetched.json()["status"] == "processing"

    async def test_a_claimed_note_is_not_claimed_twice(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
    ) -> None:
        """Two workers must never generate the same note."""
        await create_note(client, classroom["id"], teacher)
        service = note_service.NoteService(db_session)

        first = await service.claim_next_pending()
        second = await service.claim_next_pending()

        assert first is not None
        assert second is None


class TestUploads:
    async def test_presign_returns_a_url_and_asset(
        self, client: AsyncClient, teacher: dict[str, str], fake_storage: None
    ) -> None:
        response = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "lecture.m4a", "content_type": "audio/m4a"},
            headers=teacher,
        )

        assert response.status_code == 201
        body = response.json()
        assert body["upload_url"].startswith("https://storage.test/")
        assert body["content_type"] == "audio/m4a"
        assert body["asset_id"]

    async def test_rejects_an_unsupported_type(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "virus.exe", "content_type": "application/x-msdownload"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_rejects_an_oversized_file(
        self, client: AsyncClient, teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/uploads/presign",
            json={
                "filename": "huge.wav",
                "content_type": "audio/wav",
                "size_bytes": 900 * 1024 * 1024,
            },
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_audio_upload_becomes_an_audio_note(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        fake_storage: None,
        fake_gemini: None,
        db_session: Any,
    ) -> None:
        presigned = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "lecture.m4a", "content_type": "audio/m4a"},
            headers=teacher,
        )
        asset_id = presigned.json()["asset_id"]

        note = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"asset_id": asset_id},
            headers=teacher,
        )
        assert note.status_code == 202
        assert note.json()["source_type"] == "audio"

        await note_service.NoteService(db_session).process(note.json()["id"])
        fetched = await client.get(f"{API}/notes/{note.json()['id']}", headers=teacher)
        assert fetched.json()["status"] == "ready"

    async def test_cannot_attach_someone_elses_upload(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        fake_storage: None,
    ) -> None:
        presigned = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "lecture.m4a", "content_type": "audio/m4a"},
            headers=student,
        )
        asset_id = presigned.json()["asset_id"]

        stolen = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"asset_id": asset_id},
            headers=teacher,
        )
        assert stolen.status_code == 404


class TestVisibility:
    async def test_members_see_the_classroom_notes(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        await create_note(client, classroom["id"], teacher)

        listed = await client.get(
            f"{API}/classrooms/{classroom['id']}/notes", headers=student
        )
        assert listed.status_code == 200
        assert len(listed.json()) == 1

    async def test_the_list_omits_the_markdown_body(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        db_session: Any,
        fake_gemini: None,
    ) -> None:
        """Sending every note's full text to render a list would be wasteful."""
        note = await create_note(client, classroom["id"], teacher)
        await note_service.NoteService(db_session).process(note["id"])

        listed = await client.get(
            f"{API}/classrooms/{classroom['id']}/notes", headers=teacher
        )
        assert "markdown" not in listed.json()[0]

    async def test_filtering_by_date(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        await create_note(client, classroom["id"], teacher, date="2026-03-01")
        await create_note(client, classroom["id"], teacher, date="2026-03-02")

        filtered = await client.get(
            f"{API}/classrooms/{classroom['id']}/notes?date=2026-03-01", headers=teacher
        )
        assert len(filtered.json()) == 1

    async def test_non_member_cannot_list_or_read(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        outsider: dict[str, str],
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)

        listed = await client.get(
            f"{API}/classrooms/{classroom['id']}/notes", headers=outsider
        )
        assert listed.status_code == 404

        read = await client.get(f"{API}/notes/{note['id']}", headers=outsider)
        assert read.status_code == 404


class TestEditing:
    async def test_author_can_edit(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)

        edited = await client.patch(
            f"{API}/notes/{note['id']}",
            json={"title": "Corrected title"},
            headers=teacher,
        )
        assert edited.status_code == 200
        assert edited.json()["title"] == "Corrected title"

    async def test_another_student_cannot_edit(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        note = await create_note(client, classroom["id"], teacher)

        response = await client.patch(
            f"{API}/notes/{note['id']}", json={"title": "Hax"}, headers=student
        )
        assert response.status_code == 403

    async def test_a_teacher_can_edit_a_students_note(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        await client.post(
            f"{API}/classrooms/join", json={"code": classroom["code"]}, headers=student
        )
        note = await create_note(client, classroom["id"], student)

        response = await client.patch(
            f"{API}/notes/{note['id']}", json={"title": "Tidied up"}, headers=teacher
        )
        assert response.status_code == 200

    async def test_author_can_delete(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        note = await create_note(client, classroom["id"], teacher)

        deleted = await client.delete(f"{API}/notes/{note['id']}", headers=teacher)
        assert deleted.status_code == 204

        gone = await client.get(f"{API}/notes/{note['id']}", headers=teacher)
        assert gone.status_code == 404

    async def test_deleting_removes_the_stored_object(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A cascade deletes rows, not S3 objects. Those must go explicitly, or
        the free storage tier fills with files nothing references."""
        deleted_keys: list[str] = []

        monkeypatch.setattr(
            note_service.storage_service,
            "presign_upload",
            lambda **kwargs: f"https://storage.test/{kwargs['key']}",
        )
        monkeypatch.setattr(
            note_service.storage_service,
            "delete_object",
            lambda **kwargs: deleted_keys.append(kwargs["key"]),
        )

        presigned = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "lecture.m4a", "content_type": "audio/m4a"},
            headers=teacher,
        )
        note = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"asset_id": presigned.json()["asset_id"]},
            headers=teacher,
        )

        await client.delete(f"{API}/notes/{note.json()['id']}", headers=teacher)

        assert len(deleted_keys) == 1
        assert deleted_keys[0].startswith("uploads/")


class TestYoutubeIdParsing:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?feature=share&v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        ],
    )
    def test_recognised_forms(self, url: str) -> None:
        assert ai_service.extract_youtube_id(url) == "dQw4w9WgXcQ"

    @pytest.mark.parametrize(
        "url",
        ["https://vimeo.com/12345", "not a url", "https://youtube.com/watch?v=short"],
    )
    def test_rejected_forms(self, url: str) -> None:
        assert ai_service.extract_youtube_id(url) is None


class TestStorageKeys:
    def test_keys_are_namespaced_and_unique(self) -> None:
        import uuid as uuid_module

        owner = uuid_module.uuid4()
        first = storage_service.build_key(owner_id=owner, filename="lecture.m4a")
        second = storage_service.build_key(owner_id=owner, filename="lecture.m4a")

        assert first.startswith(f"uploads/{owner}/")
        assert first.endswith(".m4a")
        # Two people uploading the same filename must not collide.
        assert first != second

    def test_a_path_traversal_filename_cannot_escape(self) -> None:
        key = storage_service.build_key(
            owner_id=__import__("uuid").uuid4(), filename="../../etc/passwd"
        )
        assert ".." not in key
