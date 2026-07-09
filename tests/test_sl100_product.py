from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

import sl100_diagnose
from sl100_es import SHANGHAI_TZ
from sl100_incident import build_incident_report, combine_incident_reports
from sl100_planner import plan_query


class Sl100ProductTests(unittest.TestCase):
    def test_query_planner_extracts_service_keyword_and_hour_window(self) -> None:
        plan = plan_query(
            "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常",
            now=datetime(2026, 7, 9, 12, 0, tzinfo=SHANGHAI_TZ),
        )

        self.assertEqual(plan["primary_service"], "deviceShadow")
        self.assertIn("websocket", plan["keyword"])
        self.assertEqual(plan["from_time"], "2026-07-09 09:00")
        self.assertEqual(plan["to_time"], "2026-07-09 10:00")

    def test_incident_report_contains_required_product_fields(self) -> None:
        report = build_incident_report(
            query="deviceShadow websocket",
            analysis={
                "facts": {
                    "source": {"type": "elasticsearch", "index_pattern": "api-device-shadow-2026-07-09", "returned": 1},
                    "services": {"deviceShadow": {}},
                    "error_count": 1,
                    "timeline": [{"service": "deviceShadow", "level": "error", "message": "websocket read error"}],
                    "incidents": [{"type": "websocket_failed"}],
                },
                "diagnosis": {
                    "risk_level": "high",
                    "summary": "websocket close 1000",
                    "incidents": [{"evidence": [{"service": "deviceShadow", "line": 1, "message": "websocket read error"}]}],
                    "next_steps": ["检查客户端关闭原因"],
                },
            },
        )

        for field in [
            "incident_id",
            "query",
            "time_window",
            "services",
            "data_sources",
            "evidence",
            "timeline",
            "root_cause",
            "confidence",
            "risk_level",
            "next_actions",
            "redaction_status",
        ]:
            self.assertIn(field, report)
        self.assertEqual(report["risk_level"], "high")
        self.assertEqual(report["confidence"], "high")

    def test_empty_report_explains_no_hit(self) -> None:
        report = build_incident_report(
            query="gateway error",
            analysis={
                "facts": {"source": {"type": "elasticsearch"}, "services": {"gateway": {}}, "error_count": 0, "timeline": []},
                "diagnosis": {"risk_level": "low", "summary": "分析 1 个日志文件，发现 0 类可疑问题，错误数 0"},
            },
        )

        self.assertIn("未命中明确异常", report["root_cause"])
        self.assertTrue(report["next_actions"])

    def test_combined_report_merges_children(self) -> None:
        child = build_incident_report(
            query="pushService error",
            analysis={
                "facts": {"source": {"type": "elasticsearch"}, "services": {"pushService": {}}, "error_count": 0},
                "diagnosis": {"risk_level": "low", "summary": "none"},
            },
        )
        combined = combine_incident_reports("pushService error", [child], plan={"services": ["pushService"]})

        self.assertEqual(combined["services"], ["pushService"])
        self.assertEqual(combined["facts_summary"]["child_reports"], 1)

    def test_product_diagnose_uses_remote_fallback_when_es_empty(self) -> None:
        es_analysis = {
            "facts": {
                "source": {"type": "elasticsearch", "index_pattern": "api-device-shadow-2026-07-09", "returned": 0},
                "services": {"deviceShadow": {}},
                "error_count": 0,
                "timeline": [],
                "incidents": [],
            },
            "diagnosis": {"risk_level": "low", "summary": "empty", "incidents": [], "next_steps": []},
        }
        remote_analysis = {
            "facts": {
                "source": {"type": "remote_file", "refs": []},
                "services": {"deviceShadow": {}},
                "error_count": 1,
                "timeline": [{"service": "deviceShadow", "level": "error", "message": "websocket read error"}],
                "incidents": [{"type": "websocket_failed"}],
            },
            "diagnosis": {
                "risk_level": "high",
                "summary": "remote hit",
                "incidents": [{"evidence": [{"service": "deviceShadow", "line": 1, "message": "websocket read error"}]}],
            },
        }

        with patch("sl100_diagnose.analyze_es_logs", return_value=es_analysis), \
             patch("sl100_diagnose.analyze_remote_logs", return_value=remote_analysis):
            report = sl100_diagnose.diagnose(
                "测试说 2026-07-09 上午 9 点多 deviceShadow websocket 异常",
            )

        self.assertEqual(report["risk_level"], "high")
        self.assertGreaterEqual(len(report["data_sources"]), 2)


if __name__ == "__main__":
    unittest.main()
