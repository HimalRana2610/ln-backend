"""Phase 6: to-do list, live quizzes, push notifications and account deletion.

Time is pinned wherever it decides an outcome — a to-do's status, a quiz
answer's penalty — so the tests prove the rules instead of racing the clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import reminders
from app.models.classroom import Classroom
from app.models.note import Asset
from app.models.notification import PushToken
from app.models.post import ClassroomPost
from app.models.user import User
from app.services import me_service, push_service, quiz_service
from app.services.me_service import todo_status
from app.services.push_service import PushMessage
from tests.test_posts import FakeBucket, bucket, post, signup, upload  # noqa: F401

API = "/api/v1"
PASSWORD = "correct-horse-battery"


# -- fixtures ------------------------------------------------------------


@dataclass
class FakeSender:
    sent: list[tuple[list[str], PushMessage]] = field(default_factory=list)
    dead: set[str] = field(default_factory=set)

    async def send(self, tokens: list[str], message: PushMessage) -> set[str]:
        self.sent.append((sorted(tokens), message))
        return self.dead & set(tokens)

    def titles_for(self, token: str) -> list[str]:
        return [m.title for tokens, m in self.sent if token in tokens]


@pytest.fixture
def push(monkeypatch: pytest.MonkeyPatch) -> FakeSender:
    fake = FakeSender()
    monkeypatch.setattr(push_service, "sender", fake)
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
async def third(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "third@example.edu", "Edsger Dijkstra")


@pytest.fixture
async def outsider(client: AsyncClient) -> dict[str, str]:
    return await signup(client, "outsider@example.edu", "Ada Lovelace")


async def make_classroom(
    client: AsyncClient, owner: dict[str, str], name: str, *members: dict[str, str]
) -> dict[str, Any]:
    created = await client.post(f"{API}/classrooms", json={"name": name}, headers=owner)
    assert created.status_code == 201, created.text
    body: dict[str, Any] = created.json()
    for headers in members:
        joined = await client.post(
            f"{API}/classrooms/join", json={"code": body["code"]}, headers=headers
        )
        assert joined.status_code == 200, joined.text
    return body


@pytest.fixture
async def classroom(
    client: AsyncClient,
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
    third: dict[str, str],
) -> dict[str, Any]:
    return await make_classroom(client, teacher, "Compilers", student, classmate, third)


async def register_token(
    client: AsyncClient, headers: dict[str, str], token: str, platform: str = "android"
) -> None:
    response = await client.post(
        f"{API}/me/devices/push-token",
        json={"token": token, "platform": platform},
        headers=headers,
    )
    assert response.status_code == 204, response.text


# -- to-do -----------------------------------------------------------------


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def test_todo_status_rules() -> None:
    past, future = NOW - timedelta(minutes=1), NOW + timedelta(minutes=1)
    assert todo_status(due_date=future, submitted_at=None, now=NOW) == "assigned"
    assert todo_status(due_date=None, submitted_at=None, now=NOW) == "assigned"
    assert todo_status(due_date=past, submitted_at=None, now=NOW) == "missing"
    # Handed in late is still done — lateness is its own flag.
    assert todo_status(due_date=past, submitted_at=NOW, now=NOW) == "done"
    assert todo_status(due_date=future, submitted_at=NOW, now=NOW) == "done"


async def test_todo_spans_classrooms_with_server_computed_status(
    client: AsyncClient,
    bucket: FakeBucket,  # noqa: F811
    push: FakeSender,
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
) -> None:
    compilers = await make_classroom(client, teacher, "Compilers", student)
    networks = await make_classroom(client, classmate, "Networks", student)

    soon = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    later = (datetime.now(UTC) + timedelta(days=9)).isoformat()
    overdue = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    done = await post(client, compilers["id"], teacher, kind="assignment", title="Lexer",
                      due_date=later)
    await post(client, compilers["id"], teacher, kind="assignment", title="Parser",
               due_date=overdue)
    await post(client, networks["id"], classmate, kind="assignment", title="Sockets",
               due_date=soon)
    await post(client, networks["id"], classmate, kind="assignment", title="Essay")
    # Not assignments: never on a to-do list.
    await post(client, networks["id"], classmate, kind="announcement", title="Room change")

    asset_id = await upload(client, student)
    submitted = await client.post(
        f"{API}/posts/{done['id']}/submissions", json={"asset_id": asset_id}, headers=student
    )
    assert submitted.status_code == 201

    response = await client.get(f"{API}/me/todo", headers=student)
    assert response.status_code == 200
    items = response.json()

    # Soonest due first, undated last.
    assert [(i["title"], i["classroom_name"], i["status"]) for i in items] == [
        ("Parser", "Compilers", "missing"),
        ("Sockets", "Networks", "assigned"),
        ("Lexer", "Compilers", "done"),
        ("Essay", "Networks", "assigned"),
    ]
    assert items[2]["submitted_at"] is not None


async def test_todo_status_follows_server_clock(
    client: AsyncClient,
    push: FakeSender,
    monkeypatch: pytest.MonkeyPatch,
    teacher: dict[str, str],
    student: dict[str, str],
) -> None:
    room = await make_classroom(client, teacher, "Compilers", student)
    due = datetime.now(UTC) + timedelta(hours=1)
    await post(client, room["id"], teacher, kind="assignment", title="Lexer",
               due_date=due.isoformat())

    monkeypatch.setattr(me_service, "utcnow", lambda: due + timedelta(seconds=1))
    [item] = (await client.get(f"{API}/me/todo", headers=student)).json()
    assert item["status"] == "missing"


async def test_teachers_have_no_todo_for_classes_they_teach(
    client: AsyncClient, push: FakeSender, teacher: dict[str, str], student: dict[str, str]
) -> None:
    room = await make_classroom(client, teacher, "Compilers", student)
    await post(client, room["id"], teacher, kind="assignment", title="Lexer")

    assert (await client.get(f"{API}/me/todo", headers=teacher)).json() == []
    assert len((await client.get(f"{API}/me/todo", headers=student)).json()) == 1


# -- quizzes ---------------------------------------------------------------


QUESTION = {
    "prompt": "Which phase builds the parse tree?",
    "options": ["Lexing", "Parsing", "Codegen", "Linking"],
    "correct_option": "B",
}


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(quiz_service, "utcnow", fake)
    return fake


async def ask(
    client: AsyncClient, classroom_id: str, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    response = await client.post(
        f"{API}/classrooms/{classroom_id}/quiz/questions",
        json={**QUESTION, **overrides},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    result: dict[str, Any] = response.json()
    return result


async def answer(
    client: AsyncClient, question_id: str, headers: dict[str, str], option: str
) -> Any:
    return await client.post(
        f"{API}/quiz/questions/{question_id}/answers", json={"option": option}, headers=headers
    )


async def end(client: AsyncClient, question_id: str, headers: dict[str, str]) -> None:
    response = await client.post(f"{API}/quiz/questions/{question_id}/end", headers=headers)
    assert response.status_code == 200, response.text


async def test_student_answers_once_and_cannot_change_it(
    client: AsyncClient,
    push: FakeSender,
    clock: Clock,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    db_session: AsyncSession,
) -> None:
    question = await ask(client, classroom["id"], teacher)

    first = await answer(client, question["id"], student, "A")
    assert first.status_code == 201
    assert first.json()["is_correct"] is False

    # Switching to the right answer is refused, and the wrong one stands.
    second = await answer(client, question["id"], student, "B")
    assert second.status_code == 409

    state = (await client.get(f"{API}/classrooms/{classroom['id']}/quiz", headers=student)).json()
    assert state["active"]["my_option"] == "A"
    assert state["active"]["answer_count"] == 1


async def test_correct_option_hidden_from_students_until_ended(
    client: AsyncClient,
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
) -> None:
    question = await ask(client, classroom["id"], teacher)
    await answer(client, question["id"], student, "B")
    url = f"{API}/classrooms/{classroom['id']}/quiz"

    live = (await client.get(url, headers=student)).json()
    assert live["active"]["correct_option"] is None
    assert live["active"]["my_is_correct"] is None
    assert "Parsing" in live["active"]["options"]
    assert (await client.get(url, headers=teacher)).json()["active"]["correct_option"] == "B"

    await end(client, question["id"], teacher)
    ended = (await client.get(url, headers=student)).json()
    assert ended["active"] is None
    assert ended["recent"][0]["correct_option"] == "B"
    assert ended["recent"][0]["my_is_correct"] is True


async def test_leaderboard_ranks_by_correct_then_penalty(
    client: AsyncClient,
    push: FakeSender,
    clock: Clock,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
    third: dict[str, str],
) -> None:
    async def round_(answers: list[tuple[dict[str, str], str, int]]) -> None:
        clock.now = clock.now + timedelta(minutes=5)
        started = clock.now
        question = await ask(client, classroom["id"], teacher)
        for headers, option, after in answers:
            clock.now = started + timedelta(seconds=after)
            assert (await answer(client, question["id"], headers, option)).status_code == 201
        await end(client, question["id"], teacher)

    # Turing: 2 correct, 4s + 10s = 14s.
    # Johnson: 2 correct, 3s + 6s = 9s — same score, faster, so first.
    # Dijkstra: 1 correct, but instant, and a fast *wrong* answer adds nothing.
    await round_([(student, "B", 4), (classmate, "B", 3), (third, "A", 1)])
    await round_([(student, "B", 10), (classmate, "B", 6), (third, "B", 1)])

    state = (await client.get(f"{API}/classrooms/{classroom['id']}/quiz", headers=student)).json()
    board = [(e["rank"], e["student_name"], e["correct"], e["penalty_seconds"], e["answered"])
             for e in state["leaderboard"]]
    assert board == [
        (1, "Katherine Johnson", 2, 9, 2),
        (2, "Alan Turing", 2, 14, 2),
        (3, "Edsger Dijkstra", 1, 1, 2),
    ]
    assert state["poll_interval_seconds"] > 0


async def test_leaderboard_ties_share_a_rank(
    client: AsyncClient,
    push: FakeSender,
    clock: Clock,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
) -> None:
    question = await ask(client, classroom["id"], teacher)
    clock.now = NOW + timedelta(seconds=5)
    await answer(client, question["id"], student, "B")
    await answer(client, question["id"], classmate, "B")

    board = (await client.get(f"{API}/classrooms/{classroom['id']}/quiz", headers=teacher)).json()
    assert [e["rank"] for e in board["leaderboard"]] == [1, 1]


async def test_quiz_permissions(
    client: AsyncClient,
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    outsider: dict[str, str],
) -> None:
    url = f"{API}/classrooms/{classroom['id']}/quiz/questions"
    assert (await client.post(url, json=QUESTION, headers=student)).status_code == 403
    assert (await client.post(url, json=QUESTION, headers=outsider)).status_code == 404
    assert (
        await client.get(f"{API}/classrooms/{classroom['id']}/quiz", headers=outsider)
    ).status_code == 404

    question = await ask(client, classroom["id"], teacher)
    assert (await answer(client, question["id"], teacher, "B")).status_code == 403
    assert (await answer(client, question["id"], outsider, "B")).status_code == 404
    ended = await client.post(f"{API}/quiz/questions/{question['id']}/end", headers=student)
    assert ended.status_code == 403


async def test_one_live_question_and_no_answers_after_end(
    client: AsyncClient,
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
) -> None:
    question = await ask(client, classroom["id"], teacher)
    second = await client.post(
        f"{API}/classrooms/{classroom['id']}/quiz/questions", json=QUESTION, headers=teacher
    )
    assert second.status_code == 409

    await end(client, question["id"], teacher)
    assert (await answer(client, question["id"], student, "B")).status_code == 409
    await ask(client, classroom["id"], teacher)


@pytest.mark.parametrize(
    "body",
    [
        {**QUESTION, "options": ["a", "b", "c"]},
        {**QUESTION, "options": ["a", "b", "c", "  "]},
        {**QUESTION, "correct_option": "E"},
    ],
)
async def test_question_shape_validated(
    client: AsyncClient, classroom: dict[str, Any], teacher: dict[str, str], body: dict[str, Any]
) -> None:
    response = await client.post(
        f"{API}/classrooms/{classroom['id']}/quiz/questions", json=body, headers=teacher
    )
    assert response.status_code == 422


# -- push notifications ----------------------------------------------------


async def test_material_and_assignment_push_to_members_not_author(
    client: AsyncClient,
    bucket: FakeBucket,  # noqa: F811
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    outsider: dict[str, str],
) -> None:
    await register_token(client, teacher, "teacher-phone")
    await register_token(client, student, "student-phone")
    await register_token(client, outsider, "outsider-phone")

    asset_id = await upload(client, teacher)
    await post(client, classroom["id"], teacher, kind="material", title="Slides",
               asset_id=asset_id)
    await post(client, classroom["id"], teacher, kind="assignment", title="Lexer")
    await post(client, classroom["id"], teacher, kind="announcement", title="Hello")

    assert push.titles_for("student-phone") == ["New material", "New assignment"]
    assert push.titles_for("teacher-phone") == []
    assert push.titles_for("outsider-phone") == []
    assert push.sent[0][1].data["classroom_id"] == classroom["id"]


async def test_attendance_open_pushes_students(
    client: AsyncClient,
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
) -> None:
    await register_token(client, teacher, "teacher-phone")
    await register_token(client, student, "student-phone")

    started = await client.post(
        f"{API}/classrooms/{classroom['id']}/attendance/sessions",
        json={"date": "2026-09-13"},
        headers=teacher,
    )
    assert started.status_code == 201, started.text
    assert push.titles_for("student-phone") == ["Attendance is open"]
    assert push.titles_for("teacher-phone") == []


async def test_dead_tokens_are_pruned(
    client: AsyncClient,
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
    db_session: AsyncSession,
) -> None:
    await register_token(client, student, "uninstalled")
    await register_token(client, classmate, "alive", platform="web")
    push.dead = {"uninstalled"}

    await post(client, classroom["id"], teacher, kind="assignment", title="Lexer")
    tokens = set((await db_session.scalars(select(PushToken.token))).all())
    assert tokens == {"alive"}

    # The next send no longer tries it at all.
    await post(client, classroom["id"], teacher, kind="assignment", title="Parser")
    assert push.sent[-1][0] == ["alive"]


async def test_failed_delivery_never_fails_the_request(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
) -> None:
    class Broken:
        async def send(self, tokens: list[str], message: PushMessage) -> set[str]:
            raise RuntimeError("FCM is down")

    monkeypatch.setattr(push_service, "sender", Broken())
    await register_token(client, student, "student-phone")
    await post(client, classroom["id"], teacher, kind="assignment", title="Lexer")


async def test_token_moves_to_whoever_signed_in_last(
    client: AsyncClient,
    push: FakeSender,
    student: dict[str, str],
    classmate: dict[str, str],
    db_session: AsyncSession,
) -> None:
    await register_token(client, student, "shared-browser", platform="web")
    await register_token(client, classmate, "shared-browser", platform="web")
    rows = (await db_session.scalars(select(PushToken))).all()
    assert len(rows) == 1

    # Removing someone else's token is a no-op, not a way to silence them.
    removed = await client.post(
        f"{API}/me/devices/push-token/remove", json={"token": "shared-browser"}, headers=student
    )
    assert removed.status_code == 204
    assert await db_session.scalar(select(func.count()).select_from(PushToken)) == 1

    await client.post(
        f"{API}/me/devices/push-token/remove", json={"token": "shared-browser"}, headers=classmate
    )
    assert await db_session.scalar(select(func.count()).select_from(PushToken)) == 0


async def test_due_reminders_skip_submitted_and_send_once(
    client: AsyncClient,
    bucket: FakeBucket,  # noqa: F811
    push: FakeSender,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
    db_session: AsyncSession,
) -> None:
    await register_token(client, student, "student-phone")
    await register_token(client, classmate, "classmate-phone")
    now = datetime.now(UTC)
    due = await post(client, classroom["id"], teacher, kind="assignment", title="Lexer",
                     due_date=(now + timedelta(hours=5)).isoformat())
    await post(client, classroom["id"], teacher, kind="assignment", title="Far off",
               due_date=(now + timedelta(days=5)).isoformat())

    asset_id = await upload(client, classmate)
    await client.post(
        f"{API}/posts/{due['id']}/submissions", json={"asset_id": asset_id}, headers=classmate
    )
    push.sent.clear()

    assert await reminders.send_due_reminders(db_session, now=now) == 1
    assert push.titles_for("student-phone") == ["Due soon"]
    assert push.titles_for("classmate-phone") == []

    assert await reminders.send_due_reminders(db_session, now=now) == 0
    assert len(push.sent) == 1


# -- account deletion ------------------------------------------------------


async def test_delete_account_requires_password(
    client: AsyncClient, student: dict[str, str]
) -> None:
    wrong = await client.post(
        f"{API}/users/me/delete", json={"password": "not-it"}, headers=student
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "incorrect_password"
    assert (await client.get(f"{API}/users/me", headers=student)).status_code == 200


async def test_delete_account_removes_rows_and_every_stored_object(
    client: AsyncClient,
    bucket: FakeBucket,  # noqa: F811
    push: FakeSender,
    teacher: dict[str, str],
    student: dict[str, str],
    classmate: dict[str, str],
    db_session: AsyncSession,
) -> None:
    # The teacher owns one class and co-teaches another owned by `classmate`.
    owned = await make_classroom(client, teacher, "Compilers", student)
    other = await make_classroom(client, classmate, "Networks", teacher, student)
    members = await client.get(f"{API}/classrooms/{other['id']}/members", headers=classmate)
    teacher_id = next(m["user_id"] for m in members.json() if m["full_name"] == "Grace Hopper")
    promoted = await client.patch(
        f"{API}/classrooms/{other['id']}/members/{teacher_id}",
        json={"role": "teacher"},
        headers=classmate,
    )
    assert promoted.status_code == 200, promoted.text

    # A student's submission in the owned class, a file the teacher posted in
    # the other class, and a student's submission to that post.
    owned_post = await post(client, owned["id"], teacher, kind="assignment", title="Lexer")
    owned_sub = await upload(client, student)
    await client.post(f"{API}/posts/{owned_post['id']}/submissions",
                      json={"asset_id": owned_sub}, headers=student)

    other_file = await upload(client, teacher)
    other_post = await post(client, other["id"], teacher, kind="assignment", title="Sockets",
                            asset_id=other_file)
    other_sub = await upload(client, student)
    await client.post(f"{API}/posts/{other_post['id']}/submissions",
                      json={"asset_id": other_sub}, headers=student)

    # Something that must survive: the owner's own material in their class.
    kept_file = await upload(client, classmate)
    await post(client, other["id"], classmate, kind="material", title="RFCs", asset_id=kept_file)

    await register_token(client, teacher, "teacher-phone")

    deleted = await client.post(
        f"{API}/users/me/delete", json={"password": PASSWORD}, headers=teacher
    )
    assert deleted.status_code == 204, deleted.text

    remaining = set((await db_session.scalars(select(Asset.storage_key))).all())
    # Nothing in storage without a row, and no row without an object.
    assert remaining == set(bucket.sizes)
    assert len(remaining) == 1
    assert await db_session.scalar(select(func.count()).select_from(Classroom)) == 1
    assert await db_session.scalar(select(func.count()).select_from(ClassroomPost)) == 1
    assert await db_session.scalar(select(func.count()).select_from(PushToken)) == 0
    assert await db_session.scalar(
        select(func.count()).select_from(User).where(User.email == "teacher@example.edu")
    ) == 0

    # The old access token belongs to nobody now.
    assert (await client.get(f"{API}/users/me", headers=teacher)).status_code == 401
