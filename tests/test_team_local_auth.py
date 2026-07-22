from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from fastapi.testclient import TestClient
from sqlalchemy import select

from iot_ops_agent.web.accounts import bootstrap_admin
from iot_ops_agent.web.api import create_app
from iot_ops_agent.web.auth import MemorySecurityStore
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.models import DiagnosisJob, DiagnosisStatus, InviteToken, User, UserSession, utcnow


class LocalAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        settings = TeamSettings(
            app_env="test",
            app_url="http://testserver",
            database_url=f"sqlite:///{Path(self.tmp.name) / 'team.db'}",
            redis_url="redis://unused/0",
            auth_mode="local",
            session_secret="test-session-secret-that-is-long-enough",
        )
        self.enqueued_diagnoses: list[str] = []
        self.enqueued_notifications: list[str] = []
        self.store = MemorySecurityStore()
        self.app = create_app(
            settings,
            security_store=self.store,
            enqueue_diagnosis_fn=self.enqueued_diagnoses.append,
            enqueue_notification_fn=self.enqueued_notifications.append,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        with self.app.state.session_factory() as session:
            self.admin = bootstrap_admin(
                session,
                username="admin",
                display_name="系统管理员",
                password="correct horse battery staple",
            )
        self.csrf = self._login("admin", "correct horse battery staple")

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def _login(self, username: str, password: str) -> str:
        response = self.client.post("/api/auth/login", json={"username": username, "password": password})
        self.assertEqual(response.status_code, 200, response.text)
        me = self.client.get("/api/me")
        self.assertEqual(me.status_code, 200, me.text)
        return me.json()["csrf_token"]

    def _write_headers(self, token: str | None = None) -> dict[str, str]:
        return {"X-CSRF-Token": token or self.csrf}

    def _invite(self, username: str = "alice", role: str = "oncall") -> tuple[str, str]:
        response = self.client.post(
            "/api/admin/invites",
            headers=self._write_headers(),
            json={"username": username, "display_name": username.title(), "role": role},
        )
        self.assertEqual(response.status_code, 201, response.text)
        url = response.json()["invite_url"]
        token = urlparse(url).fragment.removeprefix("invite=")
        return token, url

    def test_invitation_is_one_time_and_only_hash_is_stored(self) -> None:
        raw_token, raw_url = self._invite()
        with self.app.state.session_factory() as session:
            invite = session.scalar(select(InviteToken).where(InviteToken.username == "alice"))
            assert invite is not None
            self.assertNotEqual(invite.token_hash, raw_token)
            self.assertNotIn(raw_token, str(invite.__dict__))
            self.assertNotIn(raw_url, str(invite.__dict__))
            admin = session.scalar(select(User).where(User.username == "admin"))
            session_record = session.scalar(select(UserSession).where(UserSession.user_id == self.admin.id))
            assert admin is not None and session_record is not None
            self.assertTrue(admin.password_hash.startswith("$argon2id$"))
            self.assertNotIn("correct horse battery staple", admin.password_hash)
            self.assertNotEqual(session_record.session_id_hash, self.client.cookies.get("sl100_session"))

        response = self.client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "an excellent long passphrase"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        repeated = self.client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "another excellent passphrase"},
        )
        self.assertEqual(repeated.status_code, 422)

    def test_expired_invitation_is_rejected(self) -> None:
        raw_token, _ = self._invite("expired-user", "viewer")
        with self.app.state.session_factory() as session:
            invite = session.scalar(select(InviteToken).where(InviteToken.username == "expired-user"))
            assert invite is not None
            invite.expires_at = utcnow() - timedelta(seconds=1)
            session.commit()
        response = self.client.post(
            "/api/auth/accept-invite",
            json={"token": raw_token, "password": "an excellent long passphrase"},
        )
        self.assertEqual(response.status_code, 422)

    def test_csrf_is_required_and_last_admin_is_protected(self) -> None:
        missing_csrf = self.client.patch(
            f"/api/admin/users/{self.admin.id}",
            json={"display_name": "Changed"},
        )
        self.assertEqual(missing_csrf.status_code, 403)

        downgrade = self.client.patch(
            f"/api/admin/users/{self.admin.id}",
            headers=self._write_headers(),
            json={"role": "viewer"},
        )
        self.assertEqual(downgrade.status_code, 409)
        self.assertIn("最后一名", downgrade.json()["detail"])

    def test_five_failures_lock_account_with_generic_error(self) -> None:
        for _ in range(5):
            response = self.client.post(
                "/api/auth/login",
                json={"username": "admin", "password": "incorrect password"},
            )
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.json()["detail"], "用户名或密码错误")
        locked = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(locked.status_code, 401)
        self.assertEqual(locked.json()["detail"], "用户名或密码错误")

    def test_password_change_revokes_session_and_old_password(self) -> None:
        response = self.client.post(
            "/api/auth/change-password",
            headers=self._write_headers(),
            json={
                "current_password": "correct horse battery staple",
                "new_password": "new correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/me").status_code, 401)
        old_login = self.client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(old_login.status_code, 401)
        self._login("admin", "new correct horse battery staple")

    def test_reset_link_changes_password_and_is_one_time(self) -> None:
        token, _ = self._invite("reset-user", "viewer")
        self.client.post(
            "/api/auth/accept-invite",
            json={"token": token, "password": "initial member passphrase"},
        )
        with self.app.state.session_factory() as session:
            member = session.scalar(select(User).where(User.username == "reset-user"))
            assert member is not None
            member_id = member.id

        response = self.client.post(
            f"/api/admin/users/{member_id}/reset-link",
            headers=self._write_headers(),
        )
        self.assertEqual(response.status_code, 200, response.text)
        reset_token = urlparse(response.json()["reset_url"]).fragment.removeprefix("reset=")
        reset = self.client.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "new_password": "replacement member passphrase"},
        )
        self.assertEqual(reset.status_code, 200, reset.text)
        repeated = self.client.post(
            "/api/auth/reset-password",
            json={"token": reset_token, "new_password": "replacement number two phrase"},
        )
        self.assertEqual(repeated.status_code, 422)
        self._login("reset-user", "replacement member passphrase")

    def test_active_diagnosis_limit_and_failed_retry_linkage(self) -> None:
        token, _ = self._invite("operator", "oncall")
        self.client.post(
            "/api/auth/accept-invite",
            json={"token": token, "password": "operator member passphrase"},
        )
        self.csrf = self._login("operator", "operator member passphrase")
        job_ids: list[str] = []
        for index in range(3):
            response = self.client.post(
                "/api/diagnoses",
                headers=self._write_headers(),
                json={"query": f"gateway error number {index}"},
            )
            self.assertEqual(response.status_code, 202, response.text)
            job_ids.append(response.json()["job"]["id"])
        blocked = self.client.post(
            "/api/diagnoses",
            headers=self._write_headers(),
            json={"query": "one diagnosis too many"},
        )
        self.assertEqual(blocked.status_code, 429)

        with self.app.state.session_factory() as session:
            failed = session.get(DiagnosisJob, job_ids[0])
            assert failed is not None
            failed.status = DiagnosisStatus.FAILED
            session.commit()
        retried = self.client.post(
            f"/api/diagnoses/{job_ids[0]}/retry",
            headers=self._write_headers(),
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        self.assertEqual(retried.json()["job"]["retry_of_id"], job_ids[0])


if __name__ == "__main__":
    unittest.main()
