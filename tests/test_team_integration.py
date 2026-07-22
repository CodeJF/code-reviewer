from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from redis import Redis
from rq import Queue, SimpleWorker

from iot_ops_agent.web.accounts import bootstrap_admin
from iot_ops_agent.web.api import create_app
from iot_ops_agent.web.auth import RedisSecurityStore
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.db import make_session_factory
from iot_ops_agent.web.models import Base
from iot_ops_agent.web.services import run_diagnosis_job
from iot_ops_agent.web.tasks import enqueue_diagnosis


@unittest.skipUnless(os.environ.get("TEAM_INTEGRATION") == "1", "requires the local PostgreSQL and Redis Compose stack")
class TeamPostgresRedisIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = TeamSettings.from_env()
        self.session_factory = make_session_factory(self.settings)
        with self.session_factory() as session:
            for table in reversed(Base.metadata.sorted_tables):
                session.execute(table.delete())
            session.commit()
            bootstrap_admin(
                session,
                username="integration-admin",
                display_name="集成测试管理员",
                password="integration admin passphrase",
            )
        self.redis = Redis.from_url(self.settings.redis_url)
        self.redis.flushdb()
        self.app = create_app(self.settings, security_store=RedisSecurityStore(self.settings.redis_url))
        self.client = TestClient(self.app)
        self.client.__enter__()

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.redis.flushdb()
        self.redis.close()
        self.session_factory.kw["bind"].dispose()

    def test_api_queue_worker_and_incident_flow(self) -> None:
        login = self.client.post(
            "/api/auth/login",
            json={"username": "integration-admin", "password": "integration admin passphrase"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        csrf = self.client.get("/api/me").json()["csrf_token"]
        headers = {"X-CSRF-Token": csrf}

        created = self.client.post(
            "/api/diagnoses",
            headers=headers,
            json={"query": "gateway token=secret-value", "no_remote": True},
        )
        self.assertEqual(created.status_code, 202, created.text)
        job_id = created.json()["job"]["id"]
        self.assertEqual(Queue("diagnosis", connection=self.redis).count, 1)
        enqueue_diagnosis(job_id)
        self.assertEqual(Queue("diagnosis", connection=self.redis).count, 1)

        def complete_job(record_id: str) -> None:
            run_diagnosis_job(
                self.app.state.session_factory,
                record_id,
                diagnosis_fn=lambda query, no_remote: {
                    "conclusion": "发现网关异常",
                    "root_cause": "payload 类型不匹配",
                    "services": ["gateway"],
                    "risk_level": "medium",
                    "evidence": [{"message": "token=secret-value"}],
                    "recommendations": ["检查上游字段类型"],
                },
            )

        with patch("iot_ops_agent.web.tasks.execute_diagnosis", side_effect=complete_job):
            SimpleWorker(["diagnosis"], connection=self.redis).work(burst=True, logging_level="WARNING")

        completed = self.client.get(f"/api/diagnoses/{job_id}")
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["job"]["status"], "completed")
        self.assertNotIn("secret-value", str(completed.json()["job"]["report"]))

        promoted = self.client.post(
            "/api/incidents",
            headers=headers,
            json={"diagnosis_id": job_id, "title": "Gateway payload 异常"},
        )
        self.assertEqual(promoted.status_code, 201, promoted.text)
        incident_id = promoted.json()["incident"]["id"]
        self.assertEqual(Queue("notifications", connection=self.redis).count, 1)
        SimpleWorker(["notifications"], connection=self.redis).work(burst=True, logging_level="WARNING")

        detail = self.client.get(f"/api/incidents/{incident_id}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["incident"]["assignee_name"], "集成测试管理员")
        self.assertEqual(detail.json()["notifications"][0]["status"], "skipped")
        self.assertEqual(self.client.get("/api/ready").status_code, 200)


if __name__ == "__main__":
    unittest.main()
