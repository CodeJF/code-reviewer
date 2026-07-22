from __future__ import annotations

import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from iot_ops_agent.web.api import create_app
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import initialize_database, make_session_factory
from iot_ops_agent.web.models import (
    AgentToolCall,
    AuditEvent,
    DiagnosisJob,
    DiagnosisStatus,
    IncidentComment,
    InviteToken,
    LoginAudit,
    NotificationDelivery,
    PasswordResetToken,
    Role,
    User,
    UserSession,
    utcnow,
)
from iot_ops_agent.web.services import purge_expired_data, record_diagnosis_feedback


class TeamApiSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = TeamSettings(
            app_env="test",
            app_url="http://testserver",
            database_url=f"sqlite:///{Path(self.tmp.name) / 'team.db'}",
            auth_mode="dev",
            session_secret="test-session-secret-that-is-long-enough",
            ai_assisted_enabled=True,
            app_version="test-build",
        )
        self.diagnosis_queue: list[str] = []
        self.notification_queue: list[str] = []
        self.app = create_app(
            self.settings,
            enqueue_diagnosis_fn=self.diagnosis_queue.append,
            enqueue_notification_fn=self.notification_queue.append,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.admin = self._headers("admin", "admin", "Admin")
        self.oncall = self._headers("alice", "oncall", "Alice")
        self.other_oncall = self._headers("charlie", "oncall", "Charlie")
        self.viewer = self._headers("bob", "viewer", "Bob")

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    @staticmethod
    def _headers(user: str, role: str, name: str) -> dict[str, str]:
        return {"X-Dev-User": user, "X-Dev-Role": role, "X-Dev-Name": name}

    def _complete_job(self, job_id: str, *, duration_ms: int = 1250) -> None:
        with self.app.state.session_factory() as session:
            job = session.get(DiagnosisJob, job_id)
            assert job is not None
            job.status = DiagnosisStatus.COMPLETED
            job.report_json = {
                "schema_version": "1.0",
                "result_status": "actionable",
                "risk_level": "medium",
                "services": ["gateway"],
                "evidence": [{"evidence_id": "ev-1", "message": "payload error"}],
            }
            job.result_status = "actionable"
            job.duration_ms = duration_ms
            job.input_tokens = 100
            job.output_tokens = 25
            job.completed_at = utcnow()
            session.add(AgentToolCall(
                diagnosis_id=job.id,
                sequence=1,
                tool_name="diagnose_sl100_incident",
                arguments_json={"query": "gateway error"},
                evidence_refs_json=["ev-1"],
                status="completed",
                duration_ms=10,
            ))
            session.commit()

    def test_health_readiness_identity_static_and_role_boundaries(self) -> None:
        health = self.client.get("/api/health", headers={"X-Request-ID": "request-123"})
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.headers["X-Request-ID"], "request-123")
        self.assertEqual(health.json()["version"], "test-build")
        self.assertEqual(self.client.get("/api/ready").json()["status"], "ready")
        me = self.client.get("/api/me", headers=self.oncall).json()
        self.assertEqual(me["user"]["display_name"], "Alice")
        self.assertTrue(me["ai_assisted_enabled"])
        self.assertEqual(self.client.get("/api/users", headers=self.viewer).status_code, 200)
        self.assertEqual(self.client.get("/api/admin/users", headers=self.viewer).status_code, 403)
        self.assertEqual(self.client.get("/api/admin/users", headers=self.admin).status_code, 200)
        self.assertEqual(self.client.get("/", headers=self.viewer).status_code, 200)
        self.assertEqual(
            self.client.get("/api/me", headers={"X-Dev-Role": "invalid"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/auth/login", json={"username": "x", "password": "x"}).status_code,
            404,
        )

    def test_diagnosis_listing_errors_pagination_and_delayed_queue(self) -> None:
        self.app.state.enqueue_diagnosis = lambda _: (_ for _ in ()).throw(RuntimeError("offline"))
        delayed = self.client.post(
            "/api/diagnoses", headers=self.oncall, json={"query": "gateway error one"}
        )
        self.assertEqual(delayed.status_code, 202)
        self.assertTrue(delayed.json()["queue_delayed"])
        first_id = delayed.json()["job"]["id"]
        self._complete_job(first_id)

        self.app.state.enqueue_diagnosis = self.diagnosis_queue.append
        second_id = self.client.post(
            "/api/diagnoses", headers=self.oncall, json={"query": "gateway error two"}
        ).json()["job"]["id"]
        self._complete_job(second_id)
        third_id = self.client.post(
            "/api/diagnoses", headers=self.oncall, json={"query": "gateway error three"}
        ).json()["job"]["id"]
        self._complete_job(third_id)

        user_id = self.client.get("/api/me", headers=self.oncall).json()["user"]["id"]
        first_page = self.client.get(
            "/api/diagnoses?status=completed&limit=1", headers=self.oncall
        ).json()
        self.assertEqual(len(first_page["items"]), 1)
        self.assertIsNotNone(first_page["next_cursor"])
        second_page = self.client.get(
            f"/api/diagnoses?created_by={user_id}&limit=2&cursor={first_page['next_cursor']}",
            headers=self.oncall,
        ).json()
        self.assertTrue(second_page["items"])

        self.assertEqual(self.client.get("/api/diagnoses/missing", headers=self.oncall).status_code, 404)
        self.assertEqual(
            self.client.post("/api/diagnoses/missing/retry", headers=self.oncall).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/diagnoses/{third_id}/retry", headers=self.oncall).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                "/api/v1/diagnoses/missing/feedback",
                headers=self.oncall,
                json={"rating": "useful"},
            ).status_code,
            404,
        )

    def test_ai_plan_ownership_consent_trace_feedback_and_metrics(self) -> None:
        created = self.client.post(
            "/api/v1/diagnoses",
            headers=self.oncall,
            json={"query": "gateway payload error", "no_remote": True},
        ).json()["job"]
        job_id = created["id"]
        self.assertEqual(
            self.client.post(
                f"/api/v1/diagnoses/{job_id}/execute",
                headers=self.other_oncall,
                json={"mode": "ai_assisted", "external_ai_consent": True},
            ).status_code,
            403,
        )
        no_consent = self.client.post(
            f"/api/v1/diagnoses/{job_id}/execute",
            headers=self.oncall,
            json={"mode": "ai_assisted", "external_ai_consent": False},
        )
        self.assertEqual(no_consent.status_code, 409)
        approved = self.client.post(
            f"/api/v1/diagnoses/{job_id}/execute",
            headers=self.oncall,
            json={"mode": "ai_assisted", "external_ai_consent": True},
        )
        self.assertEqual(approved.status_code, 202)
        self.assertEqual(approved.json()["job"]["model_id"], self.settings.agent_model_id)
        self.assertEqual(
            self.client.post(
                f"/api/v1/diagnoses/{job_id}/execute",
                headers=self.oncall,
                json={"mode": "rules"},
            ).status_code,
            409,
        )

        self._complete_job(job_id)
        feedback = self.client.post(
            f"/api/v1/diagnoses/{job_id}/feedback",
            headers=self.viewer,
            json={
                "rating": "partial",
                "evidence_correct": False,
                "corrected_incident_types": ["payload_type_mismatch", "payload_type_mismatch"],
                "note": "needs more context",
            },
        )
        self.assertEqual(feedback.status_code, 200)
        updated = self.client.post(
            f"/api/v1/diagnoses/{job_id}/feedback",
            headers=self.viewer,
            json={"rating": "useful", "evidence_correct": True},
        )
        self.assertEqual(updated.status_code, 200)
        detail = self.client.get(f"/api/v1/diagnoses/{job_id}", headers=self.viewer).json()
        self.assertEqual(detail["tool_calls"][0]["evidence_refs"], ["ev-1"])
        self.assertEqual(detail["feedback"]["rating"], "useful")

        quality = self.client.get("/api/v1/admin/quality", headers=self.admin).json()
        self.assertEqual(quality["diagnoses"]["ai_assisted"], 1)
        self.assertEqual(quality["feedback"]["useful_rate"], 1.0)
        metrics = self.client.get("/internal/metrics").text
        self.assertIn('iot_ops_diagnoses_total{status="completed",mode="ai_assisted"}', metrics)
        self.assertIn('iot_ops_feedback_total{rating="useful"} 1', metrics)
        self.assertIn('iot_ops_agent_tool_calls_total{status="completed"}', metrics)

    def test_incident_detail_filters_transitions_and_notification_retry(self) -> None:
        job_id = self.client.post(
            "/api/diagnoses", headers=self.oncall, json={"query": "gateway payload error"}
        ).json()["job"]["id"]
        self._complete_job(job_id)
        created = self.client.post(
            "/api/incidents",
            headers=self.oncall,
            json={"diagnosis_id": job_id, "title": "Gateway incident"},
        )
        self.assertEqual(created.status_code, 201)
        incident_id = created.json()["incident"]["id"]
        duplicate = self.client.post(
            "/api/incidents",
            headers=self.oncall,
            json={"diagnosis_id": job_id, "title": "Duplicate"},
        )
        self.assertEqual(duplicate.json()["incident"]["id"], incident_id)

        viewer_id = self.client.get("/api/me", headers=self.viewer).json()["user"]["id"]
        self.assertEqual(
            self.client.patch(
                f"/api/incidents/{incident_id}",
                headers=self.oncall,
                json={"assignee_id": viewer_id},
            ).status_code,
            422,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/incidents/{incident_id}",
                headers=self.oncall,
                json={"status": "mitigated"},
            ).status_code,
            409,
        )
        transitioned = self.client.patch(
            f"/api/incidents/{incident_id}",
            headers=self.oncall,
            json={"status": "investigating", "assign_to_me": True},
        )
        self.assertEqual(transitioned.status_code, 200)

        listed = self.client.get(
            f"/api/incidents?status=investigating&service=gateway&assignee_id={transitioned.json()['incident']['assignee_id']}&limit=1",
            headers=self.viewer,
        ).json()
        self.assertEqual(listed["items"][0]["id"], incident_id)
        detail = self.client.get(f"/api/incidents/{incident_id}", headers=self.viewer).json()
        self.assertTrue(detail["audit"])
        self.assertTrue(detail["notifications"])
        self.assertEqual(
            self.client.get("/api/incidents/missing", headers=self.viewer).status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                "/api/incidents/missing/comments",
                headers=self.oncall,
                json={"body": "missing"},
            ).status_code,
            404,
        )

        delivery_id = detail["notifications"][0]["id"]
        with self.app.state.session_factory() as session:
            delivery = session.get(NotificationDelivery, delivery_id)
            assert delivery is not None
            delivery.status = "delivered"
            session.commit()
        self.assertEqual(
            self.client.post(f"/api/notifications/{delivery_id}/retry", headers=self.admin).status_code,
            409,
        )
        self.assertEqual(
            self.client.post("/api/notifications/missing/retry", headers=self.admin).status_code,
            404,
        )


class ConfigurationAndRetentionTests(unittest.TestCase):
    def test_environment_loading_and_production_validation(self) -> None:
        values = {
            "APP_ENV": "production",
            "APP_URL": "https://ops.example.invalid/",
            "DATABASE_URL": "postgresql+psycopg://user:pass@db/name",
            "REDIS_URL": "redis://redis:6379/1",
            "SESSION_SECRET": "x" * 40,
            "AUTH_MODE": "local",
            "AI_ASSISTED_ENABLED": "yes",
            "AGENT_MAX_TURNS": "2",
            "AGENT_MAX_TOOL_CALLS": "4",
            "AGENT_TIMEOUT_SECONDS": "60",
            "AGENT_MAX_INPUT_TOKENS": "12000",
            "AGENT_MAX_OUTPUT_TOKENS": "2048",
            "AGENT_MAX_TOOL_RESULT_CHARS": "9000",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = TeamSettings.from_env()
            self.assertEqual(settings.app_url, "https://ops.example.invalid")
            self.assertTrue(settings.ai_assisted_enabled)
            self.assertEqual(settings.agent_max_tool_calls, 4)
            with self.assertRaisesRegex(ValueError, "ANTHROPIC_API_KEY"):
                settings.validate_runtime()
        with patch.dict(os.environ, {**values, "ANTHROPIC_API_KEY": "test-key"}, clear=True):
            TeamSettings.from_env().validate_runtime()

        invalid_cases = [
            TeamSettings(auth_mode="unsupported"),
            TeamSettings(app_env="production", auth_mode="dev"),
            TeamSettings(app_env="production", auth_mode="local", app_url="http://insecure"),
            TeamSettings(app_env="production", auth_mode="local", app_url="https://ok", database_url="sqlite:///bad"),
            TeamSettings(
                app_env="production",
                auth_mode="local",
                app_url="https://ok",
                database_url="postgresql+psycopg://db",
                redis_url="http://bad",
            ),
        ]
        for settings in invalid_cases:
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate_runtime()

    def test_retention_purges_every_expired_record_type(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        settings = TeamSettings(database_url=f"sqlite:///{Path(tmp.name) / 'retention.db'}")
        factory = make_session_factory(settings)
        initialize_database(factory)
        expired = utcnow() - timedelta(days=40)
        with factory() as session:
            user = User(subject="retention", username="retention", role=Role.ADMIN)
            session.add(user)
            session.flush()
            job = DiagnosisJob(
                created_by_id=user.id,
                query="expired",
                status=DiagnosisStatus.COMPLETED,
                report_json={"result_status": "actionable"},
                expires_at=expired,
            )
            session.add(job)
            session.flush()
            from iot_ops_agent.web.models import Incident

            incident = Incident(diagnosis_id=job.id, title="expired", created_by_id=user.id)
            session.add(incident)
            session.flush()
            session.add_all([
                IncidentComment(incident_id=incident.id, author_id=user.id, body="old", expires_at=expired),
                AgentToolCall(
                    diagnosis_id=job.id,
                    sequence=1,
                    tool_name="old",
                    expires_at=expired,
                ),
                AuditEvent(action="old", target_type="job", target_id=job.id, expires_at=expired),
                LoginAudit(username="old", expires_at=expired),
                UserSession(user_id=user.id, session_id_hash="a" * 64, expires_at=expired),
                InviteToken(
                    username="old",
                    token_hash="b" * 64,
                    created_by_id=user.id,
                    expires_at=expired - timedelta(days=31),
                ),
                PasswordResetToken(
                    user_id=user.id,
                    token_hash="c" * 64,
                    created_by_id=user.id,
                    expires_at=expired - timedelta(days=31),
                ),
            ])
            session.commit()
            result = purge_expired_data(session)
            self.assertEqual(result["reports_expired"], 1)
            self.assertEqual(result["comments_deleted"], 1)
            self.assertEqual(result["tool_calls_deleted"], 1)
            self.assertEqual(result["audit_events_deleted"], 1)
            self.assertEqual(result["login_audits_deleted"], 1)
            self.assertEqual(result["sessions_deleted"], 1)
            self.assertEqual(result["tokens_deleted"], 2)
            self.assertEqual(session.get(DiagnosisJob, job.id).status, DiagnosisStatus.EXPIRED)

            with self.assertRaisesRegex(ValueError, "completed diagnosis"):
                record_diagnosis_feedback(
                    session,
                    actor=user,
                    diagnosis=job,
                    rating="useful",
                    evidence_correct=True,
                    corrected_incident_types=[],
                    note="",
                )
        factory.kw["bind"].dispose()
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
