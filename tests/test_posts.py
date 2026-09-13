"""End-to-end tests for classroom posts, submissions and downloads.

S3 is stubbed with an in-memory bucket that records deletions. The things under
test are the permission matrix, resubmission replacing rather than duplicating,
and — the part the old app kept getting wrong — that every delete removes the
stored objects as well as the rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.note import Asset
from app.models.post import ClassroomPost, Submission
from app.services import storage_service

API = "/api/v1"


# -- fixtures ------------------------------------------------------------


async def signup(client: AsyncClient, email: str, name: str) -> dict[str, str]:
    password = "correct-horse-battery"
    response = await client.post(
        f"{API}/auth/register",
        json={"email": email, "full_name": name, "password": password},
    )
    assert response.status_code == 201, response.text

    tokens = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}


@dataclass
class FakeBucket:
    """Objects that "exist" in storage, and every key deleted from it."""

    sizes: dict[str, int] = field(default_factory=dict)
    deleted: list[str] = field(default_factory=list)
    # Keys handed out by presign that the client never actually PUT.
    never_uploaded: set[str] = field(default_factory=set)


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> FakeBucket:
    fake = FakeBucket()

    def presign_upload(*, key: str, content_type: str) -> str:
        if key not in fake.never_uploaded:
            fake.sizes[key] = 1024
        return f"https://storage.test/{key}?put=1"

    def delete_object(*, key: str) -> None:
        fake.sizes.pop(key, None)
        fake.deleted.append(key)

    monkeypatch.setattr(storage_service, "presign_upload", presign_upload)
    monkeypatch.setattr(storage_service, "object_size", lambda *, key: fake.sizes.get(key))
    monkeypatch.setattr(storage_service, "delete_object", delete_object)
    monkeypatch.setattr(
        storage_service,
        "presign_download",
        lambda *, key, filename=None: f"https://storage.test/{key}?get=1&name={filename}",
    )
    return fake


@pytest.fixture
async def teacher(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "teacher@example.edu", "Grace Hopper")


@pytest.fixture
async def student(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "student@example.edu", "Alan Turing")


@pytest.fixture
async def classmate(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "classmate@example.edu", "Katherine Johnson")


@pytest.fixture
async def outsider(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "outsider@example.edu", "Ada Lovelace")


@pytest.fixture
async def classroom(
    client: AsyncClient,
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
) -> dict[str, Any]:
    created = await client.post(
        f"{API}/classrooms", json={"name": "Operating Systems"}, headers=teacher
    )
    body: dict[str, Any] = created.json()
    for headers in (student, classmate):
        joined = await client.post(
            f"{API}/classrooms/join", json={"code": body["code"]}, headers=headers
        )
        assert joined.status_code == 200, joined.text
    return body


async def upload(
    client: AsyncClient,
    headers: dict[str, str],
    *,
    filename: str = "slides.pdf",
    content_type: str = "application/pdf",
) -> str:
    response = await client.post(
        f"{API}/uploads/presign",
        json={"filename": filename, "content_type": content_type, "purpose": "attachment"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["asset_id"])


async def post(
    client: AsyncClient, classroom_id: str, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    payload = {"kind": "announcement", "title": "Quiz on Friday", **body}
    response = await client.post(
        f"{API}/classrooms/{classroom_id}/posts", json=payload, headers=headers
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def assignment(
    client: AsyncClient, classroom_id: str, headers: dict[str, str], **body: Any
) -> dict[str, Any]:
    return await post(
        client,
        classroom_id,
        headers,
        kind="assignment",
        title="Implement a scheduler",
        due_date="2099-01-01T23:59:00+05:45",
        **body,
    )


async def submit(
    client: AsyncClient, post_id: str, headers: dict[str, str], filename: str = "answer.pdf"
) -> dict[str, Any]:
    asset_id = await upload(client, headers, filename=filename)
    response = await client.post(
        f"{API}/posts/{post_id}/submissions", json={"asset_id": asset_id}, headers=headers
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def count(db: AsyncSession, model: type[Any]) -> int:
    return int(await db.scalar(select(func.count()).select_from(model)) or 0)


# -- posting -------------------------------------------------------------


class TestPosting:
    @pytest.mark.parametrize("kind", ["material", "announcement", "assignment"])
    async def test_a_teacher_can_post_every_kind(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
        kind: str,
    ) -> None:
        body: dict[str, Any] = {"kind": kind, "title": "Week 3"}
        if kind == "material":
            body["asset_id"] = await upload(client, teacher)

        created = await post(client, classroom["id"], teacher, **body)

        assert created["kind"] == kind
        assert created["author_name"] == "Grace Hopper"

    async def test_a_student_cannot_post(
        self, client: AsyncClient, classroom: dict[str, Any], student: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "announcement", "title": "No class today!"},
            headers=student,
        )
        assert response.status_code == 403

    async def test_a_promoted_co_teacher_can_post(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        me = await client.get(f"{API}/users/me", headers=student)
        promoted = await client.patch(
            f"{API}/classrooms/{classroom['id']}/members/{me.json()['id']}",
            json={"role": "teacher"},
            headers=teacher,
        )
        assert promoted.status_code == 200

        await post(client, classroom["id"], student)

    async def test_a_material_needs_a_file(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "material", "title": "Slides"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_only_assignments_have_due_dates(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={
                "kind": "announcement",
                "title": "Midterm",
                "due_date": "2099-01-01T10:00:00Z",
            },
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_a_due_date_without_a_timezone_is_rejected(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        """A bare 23:59 in whose zone? Refusing to guess is the whole point."""
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "assignment", "title": "Lab 1", "due_date": "2099-01-01T23:59:00"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_due_dates_round_trip_as_the_same_instant(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        from datetime import datetime

        created = await assignment(client, classroom["id"], teacher)
        returned = datetime.fromisoformat(created["due_date"])
        assert returned == datetime.fromisoformat("2099-01-01T23:59:00+05:45")

    async def test_listing_filters_by_kind(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        await post(client, classroom["id"], teacher)
        await assignment(client, classroom["id"], teacher)

        everything = await client.get(f"{API}/classrooms/{classroom['id']}/posts", headers=teacher)
        only = await client.get(
            f"{API}/classrooms/{classroom['id']}/posts?kind=assignment", headers=teacher
        )

        assert len(everything.json()) == 2
        assert [p["kind"] for p in only.json()] == ["assignment"]

    async def test_students_can_read_posts(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        await post(client, classroom["id"], teacher)
        listed = await client.get(f"{API}/classrooms/{classroom['id']}/posts", headers=student)
        assert listed.status_code == 200
        assert len(listed.json()) == 1


class TestEditing:
    async def test_a_teacher_can_edit(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        created = await assignment(client, classroom["id"], teacher)

        edited = await client.patch(
            f"{API}/posts/{created['id']}",
            json={"title": "Implement a better scheduler", "due_date": "2099-02-01T00:00:00Z"},
            headers=teacher,
        )

        assert edited.status_code == 200, edited.text
        assert edited.json()["title"] == "Implement a better scheduler"
        assert edited.json()["due_date"].startswith("2099-02-01")

    async def test_a_student_cannot_edit(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        created = await post(client, classroom["id"], teacher)
        response = await client.patch(
            f"{API}/posts/{created['id']}", json={"title": "Hax"}, headers=student
        )
        assert response.status_code == 403

    async def test_a_due_date_cannot_be_added_to_an_announcement(
        self, client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str]
    ) -> None:
        created = await post(client, classroom["id"], teacher)
        response = await client.patch(
            f"{API}/posts/{created['id']}",
            json={"due_date": "2099-02-01T00:00:00Z"},
            headers=teacher,
        )
        assert response.status_code == 409

    async def test_a_student_cannot_delete(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
    ) -> None:
        created = await post(client, classroom["id"], teacher)
        response = await client.delete(f"{API}/posts/{created['id']}", headers=student)
        assert response.status_code == 403


# -- uploads -------------------------------------------------------------


class TestAttachments:
    async def test_attachments_accept_office_documents(
        self, client: AsyncClient, teacher: dict[str, str], bucket: FakeBucket
    ) -> None:
        await upload(
            client,
            teacher,
            filename="syllabus.docx",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )

    async def test_note_uploads_still_reject_them(
        self, client: AsyncClient, teacher: dict[str, str], bucket: FakeBucket
    ) -> None:
        """Gemini cannot read a .docx; the Phase 2 allowlist must not widen."""
        response = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "syllabus.docx", "content_type": "application/msword"},
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_an_executable_is_rejected(
        self, client: AsyncClient, teacher: dict[str, str], bucket: FakeBucket
    ) -> None:
        response = await client.post(
            f"{API}/uploads/presign",
            json={
                "filename": "setup.exe",
                "content_type": "application/x-msdownload",
                "purpose": "attachment",
            },
            headers=teacher,
        )
        assert response.status_code == 422

    async def test_an_unfinished_upload_cannot_be_attached(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The API never sees the PUT, so it must check storage before trusting it."""
        monkeypatch.setattr(storage_service, "object_size", lambda *, key: None)
        asset_id = await upload(client, teacher)

        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "material", "title": "Slides", "asset_id": asset_id},
            headers=teacher,
        )
        assert response.status_code == 409
        assert "not finished" in response.json()["error"]["message"]

    async def test_someone_elses_upload_cannot_be_attached(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        theirs = await upload(client, student)
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "material", "title": "Slides", "asset_id": theirs},
            headers=teacher,
        )
        assert response.status_code == 404

    async def test_one_file_cannot_back_two_posts(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        """Otherwise deleting either post would delete the other's file."""
        asset_id = await upload(client, teacher)
        await post(client, classroom["id"], teacher, kind="material", asset_id=asset_id)

        again = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "material", "title": "Copy", "asset_id": asset_id},
            headers=teacher,
        )
        assert again.status_code == 409

    async def test_the_classroom_storage_cap_is_enforced(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "classroom_storage_limit_mb", 1)
        monkeypatch.setattr(storage_service, "object_size", lambda *, key: 700 * 1024)

        first = await upload(client, teacher)
        await post(client, classroom["id"], teacher, kind="material", asset_id=first)

        second = await upload(client, teacher)
        response = await client.post(
            f"{API}/classrooms/{classroom['id']}/posts",
            json={"kind": "material", "title": "More slides", "asset_id": second},
            headers=teacher,
        )
        assert response.status_code == 409
        assert "1 MB" in response.json()["error"]["message"]

    async def test_the_measured_size_replaces_the_declared_one(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        response = await client.post(
            f"{API}/uploads/presign",
            json={
                "filename": "slides.pdf",
                "content_type": "application/pdf",
                "size_bytes": 1,
                "purpose": "attachment",
            },
            headers=teacher,
        )
        created = await post(
            client,
            classroom["id"],
            teacher,
            kind="material",
            asset_id=response.json()["asset_id"],
        )
        assert created["asset"]["size_bytes"] == 1024


# -- submissions ---------------------------------------------------------


class TestSubmissions:
    async def test_a_student_can_submit(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        submission = await submit(client, work["id"], student)

        assert submission["student_name"] == "Alan Turing"
        assert submission["asset"]["filename"] == "answer.pdf"
        assert submission["is_late"] is False

    async def test_resubmitting_replaces_rather_than_duplicates(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        first = await submit(client, work["id"], student, filename="draft.pdf")
        second = await submit(client, work["id"], student, filename="final.pdf")

        assert second["id"] == first["id"]
        assert second["asset"]["filename"] == "final.pdf"
        assert await count(db_session, Submission) == 1

        # The replaced file is gone from storage, not merely unreferenced.
        assert len(bucket.deleted) == 1
        assert await db_session.get(Asset, first["asset"]["id"]) is None

        mine = await client.get(f"{API}/posts/{work['id']}/submissions/me", headers=student)
        assert mine.json()["asset"]["filename"] == "final.pdf"

    async def test_a_teacher_cannot_submit(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        asset_id = await upload(client, teacher)
        response = await client.post(
            f"{API}/posts/{work['id']}/submissions", json={"asset_id": asset_id}, headers=teacher
        )
        assert response.status_code == 403

    async def test_only_assignments_accept_submissions(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        announcement = await post(client, classroom["id"], teacher)
        asset_id = await upload(client, student)
        response = await client.post(
            f"{API}/posts/{announcement['id']}/submissions",
            json={"asset_id": asset_id},
            headers=student,
        )
        assert response.status_code == 409

    async def test_late_work_is_accepted_and_flagged(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        overdue = await post(
            client,
            classroom["id"],
            teacher,
            kind="assignment",
            title="Lab 0",
            due_date="2000-01-01T00:00:00Z",
        )
        submission = await submit(client, overdue["id"], student)
        assert submission["is_late"] is True

    async def test_a_teacher_sees_every_submission(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        await submit(client, work["id"], student)
        await submit(client, work["id"], classmate)

        listed = await client.get(f"{API}/posts/{work['id']}/submissions", headers=teacher)

        assert listed.status_code == 200
        assert {s["student_name"] for s in listed.json()} == {"Alan Turing", "Katherine Johnson"}

        posts = await client.get(f"{API}/classrooms/{classroom['id']}/posts", headers=teacher)
        assert posts.json()[0]["submission_count"] == 2
        assert posts.json()[0]["my_submitted_at"] is None

    async def test_a_student_sees_only_their_own(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        await submit(client, work["id"], classmate)

        everyone = await client.get(f"{API}/posts/{work['id']}/submissions", headers=student)
        assert everyone.status_code == 403

        # The classmate's work does not show up as this student's.
        mine = await client.get(f"{API}/posts/{work['id']}/submissions/me", headers=student)
        assert mine.status_code == 404

        posts = await client.get(f"{API}/classrooms/{classroom['id']}/posts", headers=student)
        assert posts.json()[0]["my_submitted_at"] is None
        assert posts.json()[0]["submission_count"] is None

        await submit(client, work["id"], student)
        posts = await client.get(f"{API}/classrooms/{classroom['id']}/posts", headers=student)
        assert posts.json()[0]["my_submitted_at"] is not None


# -- downloads -----------------------------------------------------------


class TestDownloads:
    async def test_a_member_gets_a_presigned_url_not_the_bytes(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        asset_id = await upload(client, teacher, filename="week3.pdf")
        await post(client, classroom["id"], teacher, kind="material", asset_id=asset_id)

        response = await client.get(f"{API}/assets/{asset_id}/download", headers=student)

        assert response.status_code == 200
        assert response.headers["content-type"] == "application/json"
        body = response.json()
        assert body["url"].startswith("https://storage.test/")
        assert "name=week3.pdf" in body["url"]
        assert body["expires_in"] == settings.s3_presign_ttl_seconds

    async def test_a_classmate_cannot_download_your_submission(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        work = await assignment(client, classroom["id"], teacher)
        submission = await submit(client, work["id"], student)
        url = f"{API}/assets/{submission['asset']['id']}/download"

        assert (await client.get(url, headers=student)).status_code == 200
        assert (await client.get(url, headers=teacher)).status_code == 200
        assert (await client.get(url, headers=classmate)).status_code == 404

    async def test_an_unattached_upload_is_only_visible_to_its_owner(
        self,
        client: AsyncClient,
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        asset_id = await upload(client, teacher)
        url = f"{API}/assets/{asset_id}/download"

        assert (await client.get(url, headers=teacher)).status_code == 200
        assert (await client.get(url, headers=student)).status_code == 404

    def test_content_disposition_keeps_non_ascii_names(self) -> None:
        header = storage_service.content_disposition('Lecture "3" — नोट्स.pdf')
        assert header.startswith('attachment; filename="Lecture _3_ _ ')
        assert "filename*=UTF-8''Lecture%20%223%22%20%E2%80%94%20" in header


# -- non-members ---------------------------------------------------------


class TestNonMembers:
    async def test_every_route_is_a_404(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        outsider: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        """A non-member must not even learn that these things exist."""
        material_file = await upload(client, teacher)
        await post(client, classroom["id"], teacher, kind="material", asset_id=material_file)
        work = await assignment(client, classroom["id"], teacher)
        await submit(client, work["id"], student)
        outsider_file = await upload(client, outsider)

        cid, pid = classroom["id"], work["id"]
        requests = [
            ("GET", f"/classrooms/{cid}/posts", None),
            ("POST", f"/classrooms/{cid}/posts", {"kind": "announcement", "title": "Hi"}),
            ("GET", f"/posts/{pid}", None),
            ("PATCH", f"/posts/{pid}", {"title": "Hax"}),
            ("DELETE", f"/posts/{pid}", None),
            ("POST", f"/posts/{pid}/submissions", {"asset_id": outsider_file}),
            ("GET", f"/posts/{pid}/submissions", None),
            ("GET", f"/posts/{pid}/submissions/me", None),
            ("GET", f"/assets/{material_file}/download", None),
        ]

        for method, path, body in requests:
            response = await client.request(method, f"{API}{path}", json=body, headers=outsider)
            assert response.status_code == 404, f"{method} {path} → {response.status_code}"


# -- deletion ------------------------------------------------------------


class TestDeletion:
    async def test_deleting_a_post_removes_submissions_and_every_object(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        brief = await upload(client, teacher, filename="brief.pdf")
        work = await assignment(client, classroom["id"], teacher, asset_id=brief)
        await submit(client, work["id"], student)
        await submit(client, work["id"], classmate)
        assert len(bucket.sizes) == 3

        response = await client.delete(f"{API}/posts/{work['id']}", headers=teacher)

        assert response.status_code == 204
        assert bucket.sizes == {}, "an object survived its post"
        assert len(bucket.deleted) == 3
        assert await count(db_session, Submission) == 0
        assert await count(db_session, Asset) == 0
        assert (await client.get(f"{API}/posts/{work['id']}", headers=teacher)).status_code == 404

    async def test_deleting_a_classroom_leaves_no_orphaned_objects(
        self,
        client: AsyncClient,
        db_session: AsyncSession,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        bucket: FakeBucket,
    ) -> None:
        material_file = await upload(client, teacher)
        await post(client, classroom["id"], teacher, kind="material", asset_id=material_file)
        work = await assignment(client, classroom["id"], teacher)
        await submit(client, work["id"], student)

        # A Phase 2 note recording lives in the classroom too.
        note_upload = await client.post(
            f"{API}/uploads/presign",
            json={"filename": "lecture.m4a", "content_type": "audio/m4a"},
            headers=teacher,
        )
        note = await client.post(
            f"{API}/classrooms/{classroom['id']}/notes",
            json={"asset_id": note_upload.json()["asset_id"]},
            headers=teacher,
        )
        assert note.status_code == 202
        assert len(bucket.sizes) == 3

        response = await client.delete(f"{API}/classrooms/{classroom['id']}", headers=teacher)

        assert response.status_code == 204
        assert bucket.sizes == {}, f"orphaned objects: {sorted(bucket.sizes)}"
        assert await count(db_session, ClassroomPost) == 0
        assert await count(db_session, Submission) == 0
        assert await count(db_session, Asset) == 0
