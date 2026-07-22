from __future__ import annotations

import tempfile
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from iot_ops_agent.web.agent_runtime import (
    AgentPolicyError,
    _safe_json,
    build_approved_plan,
    constrain_tool_arguments,
    run_controlled_agent,
)
from iot_ops_agent.web.api import create_app
from iot_ops_agent.web.config import TeamSettings
from iot_ops_agent.web.models import DiagnosisJob, DiagnosisStatus
from iot_ops_agent.web.services import run_diagnosis_job


class ControlledAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = TeamSettings(
            app_env="test",
            app_url="http://testserver",
            database_url=f"sqlite:///{Path(self.tmp.name) / 'team.db'}",
            auth_mode="dev",
            session_secret="test-session-secret-that-is-long-enough",
        )
        self.enqueued: list[str] = []
        self.app = create_app(self.settings, enqueue_diagnosis_fn=self.enqueued.append)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.oncall = {"X-Dev-User": "operator", "X-Dev-Role": "oncall"}
        self.admin = {"X-Dev-User": "admin", "X-Dev-Role": "admin"}

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def test_v1_plan_requires_approval_before_enqueue(self) -> None:
        created = self.client.post(
            "/api/v1/diagnoses",
            headers=self.oncall,
            json={"query": "deviceShadow websocket token=secret-value", "no_remote": True},
        )
        self.assertEqual(created.status_code, 201, created.text)
        job = created.json()["job"]
        self.assertEqual(job["status"], "planned")
        self.assertEqual(self.enqueued, [])
        self.assertNotIn("secret-value", str(job["plan"]))
        self.assertFalse(job["plan"]["allow_remote"])

        executed = self.client.post(
            f"/api/v1/diagnoses/{job['id']}/execute",
            headers=self.oncall,
            json={"mode": "rules"},
        )
        self.assertEqual(executed.status_code, 202, executed.text)
        self.assertEqual(executed.json()["job"]["status"], "queued")
        self.assertEqual(self.enqueued, [job["id"]])

    def test_ai_execution_requires_enabled_setting_and_consent(self) -> None:
        created = self.client.post(
            "/api/v1/diagnoses",
            headers=self.oncall,
            json={"query": "gateway error"},
        ).json()["job"]
        blocked = self.client.post(
            f"/api/v1/diagnoses/{created['id']}/execute",
            headers=self.oncall,
            json={"mode": "ai_assisted", "external_ai_consent": True},
        )
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("not enabled", blocked.json()["detail"])

    def test_feedback_and_quality_metrics_are_persisted(self) -> None:
        created = self.client.post(
            "/api/diagnoses",
            headers=self.oncall,
            json={"query": "gateway error"},
        ).json()["job"]
        with self.app.state.session_factory() as session:
            job = session.get(DiagnosisJob, created["id"])
            assert job is not None
            job.status = DiagnosisStatus.COMPLETED
            job.report_json = {"schema_version": "1.0", "result_status": "actionable"}
            session.commit()
        response = self.client.post(
            f"/api/v1/diagnoses/{created['id']}/feedback",
            headers=self.oncall,
            json={"rating": "useful", "evidence_correct": True, "note": "token=secret-value"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        detail = self.client.get(f"/api/v1/diagnoses/{created['id']}", headers=self.oncall).json()
        self.assertEqual(detail["feedback"]["rating"], "useful")
        self.assertNotIn("secret-value", detail["feedback"]["note"])
        quality = self.client.get("/api/v1/admin/quality", headers=self.admin)
        self.assertEqual(quality.status_code, 200, quality.text)
        self.assertEqual(quality.json()["feedback"]["useful_rate"], 1.0)

    def test_tool_arguments_cannot_escape_the_approved_plan(self) -> None:
        plan = build_approved_plan("deviceShadow websocket error", no_remote=True, settings=self.settings)
        with self.assertRaises(AgentPolicyError):
            constrain_tool_arguments("analyze_es_logs", {"service": "gateway"}, plan)
        with self.assertRaises(AgentPolicyError):
            constrain_tool_arguments("search_remote_log", {"service": "deviceShadow"}, plan)
        constrained = constrain_tool_arguments(
            "analyze_es_logs",
            {"service": "deviceShadow", "from_time": "2000-01-01 00:00", "size": 1000},
            plan,
        )
        self.assertEqual(constrained["from_time"], plan["from_time"])
        self.assertEqual(constrained["size"], 80)

    def test_large_tool_result_stays_valid_json_when_truncated(self) -> None:
        serialized = _safe_json({"message": "x" * 500}, 160)
        self.assertLessEqual(len(serialized), 160)
        envelope = json.loads(serialized)
        self.assertTrue(envelope["truncated"])
        self.assertGreater(envelope["original_chars"], len(envelope["preview"]))

    def test_ai_failure_falls_back_to_existing_deterministic_pipeline(self) -> None:
        ai_settings = TeamSettings(**{**self.settings.__dict__, "ai_assisted_enabled": True})
        with self.app.state.session_factory() as session:
            user = session.query(DiagnosisJob).first()
            self.assertIsNone(user)
        created = self.client.post(
            "/api/diagnoses", headers=self.oncall, json={"query": "gateway error"}
        ).json()["job"]
        with self.app.state.session_factory() as session:
            job = session.get(DiagnosisJob, created["id"])
            assert job is not None
            job.execution_mode = "ai_assisted"
            job.plan_json = build_approved_plan(job.query, no_remote=False, settings=ai_settings)
            session.commit()

        def unavailable_agent(*args, **kwargs):
            raise RuntimeError("provider token=secret-value unavailable")

        run_diagnosis_job(
            self.app.state.session_factory,
            created["id"],
            settings=ai_settings,
            agent_fn=unavailable_agent,
            diagnosis_fn=lambda query, no_remote: {
                "schema_version": "1.0",
                "result_status": "no_evidence",
                "risk_level": "low",
                "evidence": [],
            },
        )
        detail = self.client.get(f"/api/v1/diagnoses/{created['id']}", headers=self.oncall).json()["job"]
        self.assertEqual(detail["status"], "completed")
        self.assertEqual(detail["report"]["agent_execution"]["status"], "fallback")
        self.assertNotIn("secret-value", str(detail["report"]))

    def test_controlled_agent_records_tools_and_accepts_grounded_analysis(self) -> None:
        ai_settings = TeamSettings(**{**self.settings.__dict__, "ai_assisted_enabled": True})
        plan = build_approved_plan("gateway payload error", no_remote=True, settings=ai_settings)
        evidence_id = "ev-grounded-1"
        deterministic_report = {
            "schema_version": "1.0",
            "result_status": "actionable",
            "risk_level": "medium",
            "root_cause": "规则结论",
            "next_actions": ["规则建议"],
            "evidence": [{"evidence_id": evidence_id, "message": "payload decode error"}],
        }
        responses = [
            SimpleNamespace(
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=100, output_tokens=20),
                content=[SimpleNamespace(
                    type="tool_use",
                    name="diagnose_sl100_incident",
                    input={"query": "try another service"},
                    id="tool-1",
                )],
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=120, output_tokens=40),
                content=[SimpleNamespace(
                    type="text",
                    text=(
                        '{"summary":"找到异常","root_cause":"payload 类型不匹配",'
                        '"next_actions":["检查上游编码"],"evidence_refs":["ev-grounded-1"]}'
                    ),
                )],
            ),
        ]

        class FakeMessages:
            def create(self, **kwargs):
                self.last_request = kwargs
                return responses.pop(0)

        messages = FakeMessages()
        result = run_controlled_agent(
            "gateway payload error",
            plan=plan,
            settings=ai_settings,
            client=SimpleNamespace(messages=messages),
            tool_functions={"diagnose_sl100_incident": lambda **kwargs: deterministic_report},
        )
        self.assertEqual(result["report"]["root_cause"], "payload 类型不匹配")
        self.assertEqual(result["execution"]["status"], "completed")
        self.assertEqual(result["execution"]["tool_call_count"], 1)
        self.assertEqual(result["tool_calls"][0]["evidence_refs"], [evidence_id])
        self.assertEqual(result["tool_calls"][0]["arguments"]["query"], plan["query"])

    def test_controlled_agent_rejects_ungrounded_model_claim_and_keeps_report(self) -> None:
        ai_settings = TeamSettings(**{**self.settings.__dict__, "ai_assisted_enabled": True})
        plan = build_approved_plan("gateway error", no_remote=True, settings=ai_settings)
        responses = [
            SimpleNamespace(
                stop_reason="tool_use",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                content=[SimpleNamespace(
                    type="tool_use", name="diagnose_sl100_incident", input={}, id="tool-1"
                )],
            ),
            SimpleNamespace(
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
                content=[SimpleNamespace(
                    type="text",
                    text='{"summary":"猜测","root_cause":"无证据结论","next_actions":[],"evidence_refs":["ev-invented"]}',
                )],
            ),
        ]

        class FakeMessages:
            def create(self, **kwargs):
                return responses.pop(0)

        report = {
            "schema_version": "1.0",
            "result_status": "no_evidence",
            "root_cause": "未命中明确异常",
            "evidence": [],
        }
        result = run_controlled_agent(
            "gateway error",
            plan=plan,
            settings=ai_settings,
            client=SimpleNamespace(messages=FakeMessages()),
            tool_functions={"diagnose_sl100_incident": lambda **kwargs: report},
        )
        self.assertEqual(result["execution"]["status"], "fallback")
        self.assertEqual(result["report"]["root_cause"], "未命中明确异常")


if __name__ == "__main__":
    unittest.main()
