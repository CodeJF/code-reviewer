from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from team_app.api import create_app
from team_app.config import TeamSettings
from team_app.models import DiagnosisJob, DiagnosisStatus
from team_app.services import run_diagnosis_job


class TeamWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        database_url = f"sqlite:///{Path(self.tmp.name) / 'team.db'}"
        settings = TeamSettings(
            app_env="test",
            app_url="http://testserver",
            database_url=database_url,
            auth_mode="dev",
            session_secret="test-secret",
        )
        self.enqueued_diagnoses: list[str] = []
        self.enqueued_notifications: list[str] = []
        app = create_app(
            settings,
            enqueue_diagnosis_fn=self.enqueued_diagnoses.append,
            enqueue_notification_fn=self.enqueued_notifications.append,
        )
        self.app = app
        self.client = TestClient(app)
        self.client.__enter__()
        self.oncall = {"X-Dev-User": "alice", "X-Dev-Role": "oncall", "X-Dev-Name": "Alice"}
        self.viewer = {"X-Dev-User": "bob", "X-Dev-Role": "viewer", "X-Dev-Name": "Bob"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def _complete_diagnosis(self, job_id: str) -> None:
        with self.app.state.session_factory() as session:
            job = session.get(DiagnosisJob, job_id)
            assert job is not None
            job.status = DiagnosisStatus.COMPLETED
            job.report_json = {
                "result_status": "actionable",
                "risk_level": "medium",
                "services": ["gateway"],
                "evidence": [{"message": "error decoding key payload: <REDACTED>"}],
            }
            session.commit()

    def test_oncall_can_promote_diagnosis_and_viewer_cannot_mutate(self) -> None:
        response = self.client.post("/api/diagnoses", headers=self.oncall, json={"query": "gateway payload 异常"})
        self.assertEqual(response.status_code, 202)
        job_id = response.json()["job"]["id"]
        self.assertEqual(self.enqueued_diagnoses, [job_id])
        self._complete_diagnosis(job_id)

        response = self.client.post("/api/incidents", headers=self.oncall, json={"diagnosis_id": job_id, "title": "Gateway payload 类型异常"})
        self.assertEqual(response.status_code, 201)
        incident_id = response.json()["incident"]["id"]
        self.assertEqual(len(self.enqueued_notifications), 1)

        response = self.client.patch(f"/api/incidents/{incident_id}", headers=self.viewer, json={"status": "investigating"})
        self.assertEqual(response.status_code, 403)

        response = self.client.patch(f"/api/incidents/{incident_id}", headers=self.oncall, json={"status": "investigating", "assign_to_me": True})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["incident"]["status"], "investigating")

        response = self.client.post(f"/api/incidents/{incident_id}/comments", headers=self.oncall, json={"body": "已复现，token=secret-value"})
        self.assertEqual(response.status_code, 201)
        self.assertIn("<REDACTED_TOKEN>", response.json()["comment"]["body"])
        self.assertNotIn("secret-value", response.json()["comment"]["body"])

    def test_diagnosis_worker_persists_only_a_redacted_report(self) -> None:
        response = self.client.post("/api/diagnoses", headers=self.oncall, json={"query": "gateway error", "no_remote": True})
        job_id = response.json()["job"]["id"]

        run_diagnosis_job(
            self.app.state.session_factory,
            job_id,
            diagnosis_fn=lambda query, no_remote: {
                "query": query,
                "result_status": "actionable",
                "risk_level": "medium",
                "evidence": [{"message": "token=<REDACTED_TOKEN>"}],
            },
        )

        response = self.client.get(f"/api/diagnoses/{job_id}", headers=self.oncall)
        self.assertEqual(response.json()["job"]["status"], "completed")
        self.assertIn("<REDACTED_TOKEN>", str(response.json()["job"]["report"]))

    def test_diagnosis_worker_redacts_a_report_with_raw_token_before_storing(self) -> None:
        response = self.client.post("/api/diagnoses", headers=self.oncall, json={"query": "gateway error"})
        job_id = response.json()["job"]["id"]

        run_diagnosis_job(
            self.app.state.session_factory,
            job_id,
            diagnosis_fn=lambda query, no_remote: {"evidence": [{"message": "token=secret-value"}]},
        )

        response = self.client.get(f"/api/diagnoses/{job_id}", headers=self.oncall)
        self.assertEqual(response.json()["job"]["status"], "completed")
        self.assertIn("<REDACTED_TOKEN>", str(response.json()["job"]["report"]))
        self.assertNotIn("secret-value", str(response.json()["job"]["report"]))


if __name__ == "__main__":
    unittest.main()
