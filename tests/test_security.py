"""Anti-proxy security: OTP, device binding, signed attendance, blocks, alerts, face.

Every attendance test here starts from a request that *would* be accepted and
breaks exactly one thing, so each rejection is proven to come from the rule
under test rather than from something else being wrong.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.attendance import AttendanceVerification
from app.models.security import FaceEnrollment, OtpCode, SecurityAlert, UserDevice
from app.services import attendance_service, beacon, email_service, face_service, otp_service
from tests.phone import Phone, ready_phone, verify_email

API = "/api/v1"
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


# -- fixtures ------------------------------------------------------------------


async def signup(client: AsyncClient, email: str, name: str) -> dict[str, str]:
    password = "correct-horse-battery"
    r = await client.post(
        f"{API}/auth/register", json={"email": email, "full_name": name, "password": password}
    )
    assert r.status_code == 201, r.text
    tokens = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    return {"Authorization": f"Bearer {tokens.json()['access_token']}"}


@dataclass
class Outbox:
    sent: list[dict[str, str]] = field(default_factory=list)

    def code(self) -> str:
        subject = self.sent[-1]["subject"]
        return subject.split()[0]


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> Outbox:
    box = Outbox()

    async def send(*, to: str, subject: str, body: str) -> None:
        box.sent.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(email_service, "send", send)
    return box


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    fake = Clock()
    monkeypatch.setattr(otp_service, "utcnow", lambda: fake.now)
    monkeypatch.setattr(attendance_service, "utcnow", lambda: fake.now)
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
    body: dict[str, Any] = (
        await client.post(f"{API}/classrooms", json={"name": "Networks"}, headers=teacher)
    ).json()
    for headers in (student, classmate):
        r = await client.post(
            f"{API}/classrooms/join", json={"code": body["code"]}, headers=headers
        )
        assert r.status_code == 200
    return body


async def user_id(client: AsyncClient, headers: dict[str, str]) -> str:
    return str((await client.get(f"{API}/users/me", headers=headers)).json()["id"])


# -- email verification ----------------------------------------------------------


class TestOtp:
    async def test_a_code_verifies_the_email(
        self, client: AsyncClient, student: dict[str, str], outbox: Outbox, clock: Clock
    ) -> None:
        sent = await client.post(f"{API}/auth/otp/send", headers=student)
        assert sent.status_code == 200, sent.text
        assert sent.json() == {"sent_to": "student@example.edu", "resend_after_seconds": 60}
        assert outbox.sent[0]["to"] == "student@example.edu"

        ok = await client.post(
            f"{API}/auth/otp/verify", json={"code": outbox.code()}, headers=student
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["is_email_verified"] is True

        again = await client.post(f"{API}/auth/otp/send", headers=student)
        assert again.status_code == 409

    async def test_codes_are_stored_hashed(
        self,
        client: AsyncClient,
        student: dict[str, str],
        outbox: Outbox,
        clock: Clock,
        db_session: AsyncSession,
    ) -> None:
        await client.post(f"{API}/auth/otp/send", headers=student)
        stored = (await db_session.scalars(select(OtpCode))).one()
        assert outbox.code() not in stored.code_hash
        assert len(stored.code_hash) == 64

    async def test_codes_expire(
        self, client: AsyncClient, student: dict[str, str], outbox: Outbox, clock: Clock
    ) -> None:
        await client.post(f"{API}/auth/otp/send", headers=student)
        clock.advance(minutes=10, seconds=1)
        r = await client.post(
            f"{API}/auth/otp/verify", json={"code": outbox.code()}, headers=student
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "otp_expired"

    async def test_wrong_attempts_are_counted_and_then_lock_the_code(
        self,
        client: AsyncClient,
        student: dict[str, str],
        outbox: Outbox,
        clock: Clock,
        db_session: AsyncSession,
    ) -> None:
        await client.post(f"{API}/auth/otp/send", headers=student)
        right = outbox.code()
        wrong = f"{(int(right) + 1) % 1_000_000:06d}"

        codes = []
        for _ in range(settings.otp_max_attempts):
            r = await client.post(f"{API}/auth/otp/verify", json={"code": wrong}, headers=student)
            codes.append(r.json()["error"]["code"])
        assert codes == ["otp_invalid"] * 4 + ["otp_locked"]

        # The counter survived each failed request's rollback.
        stored = (await db_session.scalars(select(OtpCode))).one()
        await db_session.refresh(stored)
        assert stored.attempts == settings.otp_max_attempts

        # Even the right code no longer works: a new one must be requested.
        r = await client.post(f"{API}/auth/otp/verify", json={"code": right}, headers=student)
        assert r.json()["error"]["code"] == "otp_locked"

    async def test_resend_is_rate_limited_and_retires_the_old_code(
        self,
        client: AsyncClient,
        student: dict[str, str],
        outbox: Outbox,
        clock: Clock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        codes = iter(["111111", "222222"])
        monkeypatch.setattr(otp_service, "new_code", lambda: next(codes))
        await client.post(f"{API}/auth/otp/send", headers=student)
        first = outbox.code()

        too_soon = await client.post(f"{API}/auth/otp/resend", headers=student)
        assert too_soon.status_code == 429
        assert too_soon.headers["retry-after"] == "60"

        clock.advance(seconds=61)
        assert (await client.post(f"{API}/auth/otp/resend", headers=student)).status_code == 200
        old = await client.post(f"{API}/auth/otp/verify", json={"code": first}, headers=student)
        assert old.json()["error"]["code"] == "otp_invalid"
        new = await client.post(
            f"{API}/auth/otp/verify", json={"code": outbox.code()}, headers=student
        )
        assert new.status_code == 200

    async def test_at_most_five_codes_an_hour(
        self, client: AsyncClient, student: dict[str, str], outbox: Outbox, clock: Clock
    ) -> None:
        for _ in range(settings.otp_max_per_hour):
            assert (await client.post(f"{API}/auth/otp/send", headers=student)).status_code == 200
            clock.advance(seconds=61)
        limited = await client.post(f"{API}/auth/otp/send", headers=student)
        assert limited.status_code == 429
        assert len(outbox.sent) == settings.otp_max_per_hour

    async def test_malformed_codes_are_rejected_before_counting(
        self, client: AsyncClient, student: dict[str, str]
    ) -> None:
        for bad in ("12345", "1234567", "abcdef"):
            r = await client.post(f"{API}/auth/otp/verify", json={"code": bad}, headers=student)
            assert r.status_code == 422


# -- device binding --------------------------------------------------------------


class TestDevices:
    async def test_binding_and_checking_in_again(
        self, client: AsyncClient, student: dict[str, str], db_session: AsyncSession
    ) -> None:
        phone = Phone(headers=student, user_id=uuid.UUID(await user_id(client, student)))
        first = await phone.register(client)
        again = await phone.register(client)
        assert first.status_code == again.status_code == 200
        assert first.json()["id"] == again.json()["id"]

        status = (await client.get(f"{API}/users/me/security", headers=student)).json()
        assert status["device"]["id"] == first.json()["id"]
        assert status["email_verified"] is False

    async def test_a_second_phone_is_refused_and_alerts_teachers(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        uid = uuid.UUID(await user_id(client, student))
        assert (await Phone(headers=student, user_id=uid).register(client)).status_code == 200

        second = await Phone(headers=student, user_id=uid).register(client)
        assert second.status_code == 409

        alerts = (await client.get(f"{API}/security/alerts", headers=teacher)).json()
        assert [(a["type"], a["severity"]) for a in alerts] == [("multi_device", "medium")]
        assert alerts[0]["student_name"] == "Alan Turing"
        # Still bound to the first phone only.
        assert await db_session.scalar(select(func.count()).select_from(UserDevice)) == 1

    async def test_one_phone_cannot_serve_two_students(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
    ) -> None:
        mine = Phone(headers=student, user_id=uuid.UUID(await user_id(client, student)))
        assert (await mine.register(client)).status_code == 200

        # The same handset — same key and fingerprint — signed in as a friend.
        friend = Phone(
            headers=classmate,
            user_id=uuid.UUID(await user_id(client, classmate)),
            key=mine.key,
            fingerprint=mine.fingerprint,
        )
        r = await friend.register(client)
        assert r.status_code == 409
        alerts = (await client.get(f"{API}/security/alerts", headers=teacher)).json()
        assert [(a["type"], a["student_name"]) for a in alerts] == [
            ("shared_device", "Katherine Johnson")
        ]

    async def test_same_fingerprint_with_a_fresh_key_is_still_the_same_phone(
        self, client: AsyncClient, student: dict[str, str], classmate: dict[str, str]
    ) -> None:
        """Reinstalling the app makes a new key, but not a new phone."""
        mine = Phone(headers=student, user_id=uuid.UUID(await user_id(client, student)))
        await mine.register(client)
        reinstall = Phone(
            headers=classmate,
            user_id=uuid.UUID(await user_id(client, classmate)),
            fingerprint=mine.fingerprint,
        )
        assert (await reinstall.register(client)).status_code == 409

    async def test_rejects_a_key_that_is_not_ed25519(
        self, client: AsyncClient, student: dict[str, str]
    ) -> None:
        r = await client.post(
            f"{API}/devices/register",
            json={
                "public_key": base64.b64encode(b"x" * 31).decode(),
                "fingerprint_hash": "a" * 64,
                "platform": "android",
            },
            headers=student,
        )
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_public_key"


# -- signed attendance ---------------------------------------------------------


@dataclass
class Lecture:
    session: dict[str, Any]
    phone: Phone
    evidence: list[dict[str, Any]]

    def body(self, **overrides: Any) -> dict[str, Any]:
        issued_at = overrides.pop("issued_at", int(NOW.timestamp()) + 120)
        return self.phone.signed_body(
            session_id=self.session["id"],
            observations=self.evidence,
            issued_at=issued_at,
            **overrides,
        )


@pytest.fixture
async def lecture(
    client: AsyncClient,
    classroom: dict[str, Any],
    teacher: dict[str, str],
    student: dict[str, str],
    db_session: AsyncSession,
    clock: Clock,
) -> Lecture:
    """An open session at minute 2, and a student whose request would pass."""
    phone = await ready_phone(client, db_session, student)
    session = (
        await client.post(
            f"{API}/classrooms/{classroom['id']}/attendance/sessions",
            json={"date": "2026-09-14", "threshold_minutes": 1},
            headers=teacher,
        )
    ).json()
    clock.advance(minutes=2)  # window 4
    sid = uuid.UUID(session["id"])
    evidence = [
        {
            "window": w,
            "token": beacon.token(secret=session["beacon_secret"], session_id=sid, window=w),
            "rssi": -65,
            "hop": 0,
        }
        for w in range(5)
    ]
    return Lecture(session=session, phone=phone, evidence=evidence)


async def submit(
    client: AsyncClient, lecture: Lecture, headers: dict[str, str], body: dict[str, Any]
) -> dict[str, Any]:
    r = await client.post(
        f"{API}/attendance/sessions/{lecture.session['id']}/verify", json=body, headers=headers
    )
    assert r.status_code == 200, r.text
    result: dict[str, Any] = r.json()
    return result


async def reasons(db: AsyncSession) -> list[str | None]:
    rows = await db.scalars(select(AttendanceVerification.rejection_reason))
    return list(rows.all())


class TestSignedAttendance:
    async def test_a_valid_signed_request_is_accepted(
        self, client: AsyncClient, lecture: Lecture, student: dict[str, str]
    ) -> None:
        result = await submit(client, lecture, student, lecture.body())
        assert result["accepted"] is True, result

    async def test_unverified_email_is_rejected(
        self,
        client: AsyncClient,
        lecture: Lecture,
        classmate: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        uid = uuid.UUID(await user_id(client, classmate))
        phone = Phone(headers=classmate, user_id=uid)
        await phone.register(client)
        body = Lecture(lecture.session, phone, lecture.evidence).body()
        result = await submit(client, lecture, classmate, body)
        assert result["reason"] == "email_not_verified"
        assert await reasons(db_session) == ["email_not_verified"]

    async def test_an_unsigned_request_is_rejected_and_recorded(
        self,
        client: AsyncClient,
        lecture: Lecture,
        student: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        result = await submit(client, lecture, student, {"observations": lecture.evidence})
        assert result["reason"] == "unsigned"
        row = (await db_session.scalars(select(AttendanceVerification))).one()
        # The beacon evidence was still judged: the phone *was* in range.
        assert (row.accepted, row.valid_windows) == (False, 5)

    async def test_a_badly_signed_request_is_rejected_and_alerts(
        self,
        client: AsyncClient,
        lecture: Lecture,
        student: dict[str, str],
        teacher: dict[str, str],
    ) -> None:
        body = lecture.body()
        body["signature"] = base64.b64encode(secrets.token_bytes(64)).decode()
        result = await submit(client, lecture, student, body)
        assert result["reason"] == "invalid_signature"
        alerts = (await client.get(f"{API}/security/alerts", headers=teacher)).json()
        assert [(a["type"], a["severity"]) for a in alerts] == [("invalid_signature", "critical")]

    async def test_evidence_changed_after_signing_fails_the_signature(
        self, client: AsyncClient, lecture: Lecture, student: dict[str, str]
    ) -> None:
        """Sign weak evidence, then swap in strong readings."""
        weak = [{**o, "rssi": -95} for o in lecture.evidence]
        body = Lecture(lecture.session, lecture.phone, weak).body()
        body["observations"] = lecture.evidence
        result = await submit(client, lecture, student, body)
        assert result["reason"] == "invalid_signature"

    async def test_a_signature_for_another_student_does_not_transfer(
        self,
        client: AsyncClient,
        lecture: Lecture,
        classmate: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        """A friend's genuine signed request, submitted under your own login."""
        friend = await ready_phone(client, db_session, classmate)
        body = lecture.body()  # signed by the student's phone for the student
        body["device_id"] = str(friend.device_id)
        result = await submit(client, lecture, classmate, body)
        assert result["reason"] == "invalid_signature"

    async def test_a_request_naming_someone_elses_device_is_wrong_device(
        self,
        client: AsyncClient,
        lecture: Lecture,
        classmate: dict[str, str],
        teacher: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        await verify_email(db_session, classmate, client)
        # No phone of their own; they borrow the student's device id and key.
        borrowed = Phone(
            headers=classmate,
            user_id=uuid.UUID(await user_id(client, classmate)),
            key=lecture.phone.key,
            device_id=lecture.phone.device_id,
        )
        body = Lecture(lecture.session, borrowed, lecture.evidence).body()
        result = await submit(client, lecture, classmate, body)
        assert result["reason"] == "wrong_device"
        alerts = (await client.get(f"{API}/security/alerts", headers=teacher)).json()
        assert alerts[0]["type"] == "wrong_device"
        assert alerts[0]["severity"] == "critical"

    async def test_a_replayed_request_is_rejected(
        self,
        client: AsyncClient,
        lecture: Lecture,
        student: dict[str, str],
        teacher: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        body = lecture.body(biometric=False)  # rejected, so it can be retried
        assert (await submit(client, lecture, student, body))["reason"] == "biometric_required"
        replay = await submit(client, lecture, student, body)
        assert replay["reason"] == "replayed"

    async def test_an_accepted_request_cannot_be_replayed_after_a_correction(
        self,
        client: AsyncClient,
        lecture: Lecture,
        student: dict[str, str],
        teacher: dict[str, str],
    ) -> None:
        """Marked present, set back to absent by a teacher, then the old request resent."""
        body = lecture.body()
        first = await submit(client, lecture, student, body)
        assert first["accepted"] is True
        await client.patch(
            f"{API}/attendance/records/{first['record']['id']}",
            json={"status": "absent"},
            headers=teacher,
        )
        replay = await submit(client, lecture, student, body)
        assert replay["reason"] == "replayed"

    @pytest.mark.parametrize("skew", [-121, 121])
    async def test_an_old_or_future_request_is_stale(
        self, client: AsyncClient, lecture: Lecture, student: dict[str, str], skew: int
    ) -> None:
        now = int(NOW.timestamp()) + 120
        result = await submit(client, lecture, student, lecture.body(issued_at=now + skew))
        assert result["reason"] == "stale_request"

    async def test_biometric_confirmation_is_required(
        self, client: AsyncClient, lecture: Lecture, student: dict[str, str]
    ) -> None:
        result = await submit(client, lecture, student, lecture.body(biometric=False))
        assert result["reason"] == "biometric_required"

    async def test_claiming_biometrics_without_signing_that_claim_fails(
        self, client: AsyncClient, lecture: Lecture, student: dict[str, str]
    ) -> None:
        body = lecture.body(biometric=False)
        body["biometric_verified"] = True
        result = await submit(client, lecture, student, body)
        assert result["reason"] == "invalid_signature"

    async def test_after_a_reset_the_old_phone_is_the_wrong_device(
        self,
        client: AsyncClient,
        lecture: Lecture,
        classroom: dict[str, Any],
        student: dict[str, str],
        teacher: dict[str, str],
    ) -> None:
        sid = await user_id(client, student)
        r = await client.post(
            f"{API}/classrooms/{classroom['id']}/students/{sid}/reset-enrollment", headers=teacher
        )
        assert r.status_code == 204
        result = await submit(client, lecture, student, lecture.body())
        assert result["reason"] == "wrong_device"

        # A new phone binds, and works.
        new_phone = Phone(headers=student, user_id=uuid.UUID(sid))
        assert (await new_phone.register(client)).status_code == 200
        ok = await submit(
            client, lecture, student, Lecture(lecture.session, new_phone, lecture.evidence).body()
        )
        assert ok["accepted"] is True


# -- blocks and teacher tools ----------------------------------------------------


class TestTeacherTools:
    async def test_a_blocked_student_cannot_mark_and_sees_why(
        self,
        client: AsyncClient,
        lecture: Lecture,
        classroom: dict[str, Any],
        student: dict[str, str],
        teacher: dict[str, str],
    ) -> None:
        sid = await user_id(client, student)
        url = f"{API}/classrooms/{classroom['id']}/students/{sid}/block"
        r = await client.post(
            url, json={"blocked": True, "reason": "Marked from home"}, headers=teacher
        )
        assert r.status_code == 204

        status = (await client.get(f"{API}/users/me/security", headers=student)).json()
        assert status["blocks"][0]["classroom_name"] == "Networks"
        assert status["blocks"][0]["reason"] == "Marked from home"

        result = await submit(client, lecture, student, lecture.body())
        assert result["reason"] == "student_blocked"

        await client.post(url, json={"blocked": False}, headers=teacher)
        assert (await client.get(f"{API}/users/me/security", headers=student)).json()[
            "blocks"
        ] == []
        assert (await submit(client, lecture, student, lecture.body()))["accepted"] is True

    async def test_permissions(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        classmate: dict[str, str],
        outsider: dict[str, str],
    ) -> None:
        cid = classroom["id"]
        sid, tid = await user_id(client, classmate), await user_id(client, teacher)
        calls = [
            ("GET", f"/classrooms/{cid}/students/security", None),
            ("POST", f"/classrooms/{cid}/students/{sid}/block", {"blocked": True}),
            ("POST", f"/classrooms/{cid}/students/{sid}/reset-enrollment", None),
            ("GET", f"/security/alerts?classroom_id={cid}", None),
        ]
        for method, path, body in calls:
            as_student = await client.request(method, API + path, json=body, headers=student)
            as_outsider = await client.request(method, API + path, json=body, headers=outsider)
            assert (as_student.status_code, as_outsider.status_code) == (403, 404), path

        # A teacher is not a "student" to be blocked.
        r = await client.post(
            f"{API}/classrooms/{cid}/students/{tid}/block", json={"blocked": True}, headers=teacher
        )
        assert r.status_code == 404

    async def test_student_security_overview(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        await ready_phone(client, db_session, student)
        rows = (
            await client.get(
                f"{API}/classrooms/{classroom['id']}/students/security", headers=teacher
            )
        ).json()
        by_name = {r["full_name"]: r for r in rows}
        assert by_name["Alan Turing"]["email_verified"] is True
        assert by_name["Alan Turing"]["device"]["model"] == "Test Phone"
        assert by_name["Katherine Johnson"]["device"] is None
        assert all("public_key" not in r["device"] for r in rows if r["device"])

    async def test_alerts_are_scoped_to_your_classes_and_can_be_dismissed(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        outsider: dict[str, str],
    ) -> None:
        uid = uuid.UUID(await user_id(client, student))
        await Phone(headers=student, user_id=uid).register(client)
        await Phone(headers=student, user_id=uid).register(client)

        # Another teacher with their own class sees nothing of this.
        await client.post(f"{API}/classrooms", json={"name": "Other"}, headers=outsider)
        assert (await client.get(f"{API}/security/alerts", headers=outsider)).json() == []

        [alert] = (await client.get(f"{API}/security/alerts?unread=true", headers=teacher)).json()
        assert (
            await client.post(f"{API}/security/alerts/{alert['id']}/read", headers=outsider)
        ).status_code == 404
        assert (
            await client.post(f"{API}/security/alerts/{alert['id']}/read", headers=teacher)
        ).status_code == 204
        assert (
            await client.get(f"{API}/security/alerts?unread=true", headers=teacher)
        ).json() == []

    async def test_an_alert_reaches_every_class_the_student_attends(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        outsider: dict[str, str],
        db_session: AsyncSession,
    ) -> None:
        other = (
            await client.post(f"{API}/classrooms", json={"name": "Maths"}, headers=outsider)
        ).json()
        await client.post(f"{API}/classrooms/join", json={"code": other["code"]}, headers=student)

        uid = uuid.UUID(await user_id(client, student))
        await Phone(headers=student, user_id=uid).register(client)
        await Phone(headers=student, user_id=uid).register(client)

        assert await db_session.scalar(select(func.count()).select_from(SecurityAlert)) == 2
        assert len((await client.get(f"{API}/security/alerts", headers=outsider)).json()) == 1
        assert len((await client.get(f"{API}/security/alerts", headers=teacher)).json()) == 1


# -- face ------------------------------------------------------------------------------


def _vector(seed: int, dims: int = 128) -> list[float]:
    import random

    rng = random.Random(seed)  # noqa: S311 - reproducible fake embeddings, not secrets
    return [rng.uniform(-1, 1) for _ in range(dims)]


@pytest.fixture
def faces(monkeypatch: pytest.MonkeyPatch) -> dict[bytes, list[float]]:
    """Photo bytes → the embedding the fake service returns for them."""
    table: dict[bytes, list[float]] = {}
    monkeypatch.setattr(settings, "face_embedding_url", "https://faces.test/embed")

    async def embed(image: bytes, *, content_type: str) -> list[float]:
        return table[image]

    monkeypatch.setattr(face_service, "embed", embed)
    return table


def _photos(prefix: bytes) -> dict[str, tuple[str, bytes, str]]:
    return {
        pose: (f"{pose}.jpg", prefix + pose.encode(), "image/jpeg")
        for pose in ("front", "left", "right")
    }


class TestFace:
    async def test_unavailable_without_a_configured_service(
        self, client: AsyncClient, student: dict[str, str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "face_embedding_url", "")
        status = (await client.get(f"{API}/face/status", headers=student)).json()
        assert status == {"available": False, "enrolled": False, "enrolled_at": None}
        r = await client.post(
            f"{API}/face/enrollment", files=_photos(b"x"), data={"consent": "true"}, headers=student
        )
        assert r.status_code == 503

    async def test_enrolment_needs_consent_and_three_poses(
        self, client: AsyncClient, student: dict[str, str], faces: dict[bytes, list[float]]
    ) -> None:
        no_consent = await client.post(
            f"{API}/face/enrollment", files=_photos(b"a"), headers=student
        )
        assert no_consent.json()["error"]["code"] == "consent_required"

        two = _photos(b"a")
        del two["right"]
        missing = await client.post(
            f"{API}/face/enrollment", files=two, data={"consent": "true"}, headers=student
        )
        assert missing.status_code == 422

    async def test_only_embeddings_are_stored_and_matching_works(
        self,
        client: AsyncClient,
        student: dict[str, str],
        faces: dict[bytes, list[float]],
        db_session: AsyncSession,
    ) -> None:
        alan = _vector(1)
        for pose, noise in (("front", 0.0), ("left", 0.05), ("right", 0.05)):
            faces[b"alan" + pose.encode()] = [x + noise for x in alan]
        faces[b"alan-today"] = [x + 0.02 for x in alan]
        faces[b"stranger"] = _vector(99)

        r = await client.post(
            f"{API}/face/enrollment",
            files=_photos(b"alan"),
            data={"consent": "true"},
            headers=student,
        )
        assert r.status_code == 200, r.text
        assert r.json()["enrolled"] is True

        row = (await db_session.scalars(select(FaceEnrollment))).one()
        assert set(row.embeddings) == {"front", "left", "right"}
        assert all(isinstance(x, float) for x in row.embeddings["front"])
        assert b"alan" not in repr(row.__dict__).encode()

        match = await client.post(
            f"{API}/face/verify",
            files={"image": ("f.jpg", b"alan-today", "image/jpeg")},
            headers=student,
        )
        assert match.json()["matched"] is True
        other = await client.post(
            f"{API}/face/verify",
            files={"image": ("f.jpg", b"stranger", "image/jpeg")},
            headers=student,
        )
        assert other.json()["matched"] is False

    async def test_a_person_can_delete_their_face_data_and_a_reset_deletes_it_too(
        self,
        client: AsyncClient,
        classroom: dict[str, Any],
        teacher: dict[str, str],
        student: dict[str, str],
        faces: dict[bytes, list[float]],
        db_session: AsyncSession,
    ) -> None:
        for pose in ("front", "left", "right"):
            faces[b"a" + pose.encode()] = _vector(7)

        async def enrol() -> None:
            r = await client.post(
                f"{API}/face/enrollment",
                files=_photos(b"a"),
                data={"consent": "true"},
                headers=student,
            )
            assert r.status_code == 200

        await enrol()
        assert (await client.delete(f"{API}/face/enrollment", headers=student)).status_code == 204
        assert await db_session.scalar(select(func.count()).select_from(FaceEnrollment)) == 0

        await enrol()
        sid = await user_id(client, student)
        await client.post(
            f"{API}/classrooms/{classroom['id']}/students/{sid}/reset-enrollment", headers=teacher
        )
        assert await db_session.scalar(select(func.count()).select_from(FaceEnrollment)) == 0

    async def test_non_images_are_refused(
        self, client: AsyncClient, student: dict[str, str], faces: dict[bytes, list[float]]
    ) -> None:
        files = {
            pose: (f"{pose}.txt", b"hello", "text/plain") for pose in ("front", "left", "right")
        }
        r = await client.post(
            f"{API}/face/enrollment", files=files, data={"consent": "true"}, headers=student
        )
        assert r.json()["error"]["code"] == "invalid_image"

    def test_embeddings_are_validated(self) -> None:
        from app.core.exceptions import ValidationFailedError

        for bad in ([], [0.0] * 128, [1.0] * 10, ["x"] * 128, None, [float("nan")] * 128):
            with pytest.raises(ValidationFailedError):
                face_service.validate_embedding(bad)
        assert face_service.cosine_similarity([1, 0], [1, 0]) == 1.0
        assert face_service.cosine_similarity([1, 0], [0, 1]) == 0.0


# -- the signing contract, pinned for the mobile client ---------------------------------


def test_signing_vector_is_pinned_so_the_mobile_signer_can_match_it() -> None:
    """ln-app/test/attendance_signer_test.dart checks the same values.

    Ed25519 signatures are deterministic, so one fixed key and message pins
    the whole format: field order, newlines, the observation digest.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app.schemas.attendance import Observation
    from app.services import signing

    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    observations = [
        Observation(window=3, token="0011223344556677", rssi=-70, hop=1),
        Observation(window=4, token="8899aabbccddeeff", rssi=-65, hop=0),
    ]
    message = signing.message(
        session_id=uuid.UUID("12345678-1234-5678-1234-567812345678"),
        user_id=uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"),
        device_id=uuid.UUID("11111111-2222-3333-4444-555555555555"),
        nonce="nonce-0123456789abcdef",
        issued_at=1789376400,
        biometric=True,
        observations=observations,
    )
    signature = base64.b64encode(key.sign(message)).decode()

    assert signing.observations_digest(observations) == (
        "5abcc723b65a0ca97a8a4e622c96cb428c59101a7c579c7e6da2b984f40f663d"
    )
    assert signature == (
        "iBVK7P/zGI07qjZbKElKnNW7t16LbH87vnriZFnyJsh7mPzPWkOtZ4k8ayQ8BeLIwNXWm2Zwl21a1DvvcRzFBA=="
    )
    assert signing.verify(
        public_key_b64="A6EHv/POEL4dcN0Y50vAmWfk1jCbpQ1fHdyGZBJVMbg=",
        signature_b64=signature,
        signed=message,
    )
