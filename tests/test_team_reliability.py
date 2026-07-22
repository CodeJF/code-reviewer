from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import initialize_database, make_session_factory
from iot_ops_agent.web.models import (
    DiagnosisJob,
    DiagnosisStatus,
    Incident,
    NotificationDelivery,
    Role,
    User,
    utcnow,
)
from iot_ops_agent.web.notifications import deliver_feishu
from iot_ops_agent.web.reconciler import reconcile_once


class TeamReliabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = TeamSettings(
            app_env="test",
            database_url=f"sqlite:///{Path(self.tmp.name) / 'team.db'}",
            auth_mode="dev",
            session_secret="test-session-secret-that-is-long-enough",
        )
        self.session_factory = make_session_factory(self.settings)
        initialize_database(self.session_factory)
        with self.session_factory() as session:
            user = User(
                subject="dev:reliability",
                username="reliability",
                display_name="Reliability",
                role=Role.ADMIN,
            )
            session.add(user)
            session.flush()
            job = DiagnosisJob(
                created_by_id=user.id,
                query="stuck task",
                status=DiagnosisStatus.RUNNING,
                updated_at=utcnow() - timedelta(minutes=11),
            )
            session.add(job)
            session.flush()
            incident = Incident(
                diagnosis_id=job.id,
                title="Retry notification",
                created_by_id=user.id,
                assignee_id=user.id,
            )
            session.add(incident)
            session.flush()
            delivery = NotificationDelivery(
                incident_id=incident.id,
                event_type="created",
                status="queued",
                attempts=0,
                next_attempt_at=None,
            )
            session.add(delivery)
            session.commit()
            self.job_id = job.id
            self.incident_id = incident.id
            self.delivery_id = delivery.id

    def tearDown(self) -> None:
        self.session_factory.kw["bind"].dispose()
        self.tmp.cleanup()

    def test_reconciler_recovers_stuck_diagnosis_and_due_notification(self) -> None:
        diagnosis_ids: list[str] = []
        notification_ids: list[str] = []
        result = reconcile_once(
            self.session_factory,
            enqueue_diagnosis_fn=diagnosis_ids.append,
            enqueue_notification_fn=notification_ids.append,
        )
        self.assertEqual(result, {"diagnoses": 1, "notifications": 1})
        self.assertEqual(diagnosis_ids, [self.job_id])
        self.assertEqual(notification_ids, [self.delivery_id])
        with self.session_factory() as session:
            job = session.get(DiagnosisJob, self.job_id)
            delivery = session.get(NotificationDelivery, self.delivery_id)
            assert job is not None and delivery is not None
            self.assertEqual(job.status, DiagnosisStatus.QUEUED)
            self.assertEqual(delivery.status, "queued")

    def test_notification_disabled_is_visible_without_network_request(self) -> None:
        with self.session_factory() as session:
            incident = session.get(Incident, self.incident_id)
            delivery = session.get(NotificationDelivery, self.delivery_id)
            assert incident is not None and delivery is not None
            deliver_feishu(session, settings=self.settings, delivery=delivery, incident=incident)
            self.assertEqual(delivery.status, "skipped")
            self.assertEqual(delivery.error_text, "飞书通知未启用")

    def test_notification_failure_records_retry_schedule_and_redacted_error(self) -> None:
        settings = TeamSettings(
            app_env="test",
            app_url="http://testserver",
            database_url=self.settings.database_url,
            auth_mode="dev",
            session_secret="test-session-secret-that-is-long-enough",
            feishu_webhook_url="https://notification.example.invalid/hook",
        )
        with self.session_factory() as session:
            incident = session.get(Incident, self.incident_id)
            delivery = session.get(NotificationDelivery, self.delivery_id)
            assert incident is not None and delivery is not None
            delivery.attempts = 0
            with patch("urllib.request.urlopen", side_effect=RuntimeError("token=secret-value")):
                for attempt in range(1, 5):
                    with self.assertRaises(RuntimeError):
                        deliver_feishu(session, settings=settings, delivery=delivery, incident=incident)
                    self.assertEqual(delivery.attempts, attempt)
                    if attempt < 4:
                        self.assertEqual(delivery.status, "retrying")
                        self.assertIsNotNone(delivery.next_attempt_at)
            self.assertEqual(delivery.status, "failed")
            self.assertIsNone(delivery.next_attempt_at)
            self.assertNotIn("secret-value", delivery.error_text)


if __name__ == "__main__":
    unittest.main()
