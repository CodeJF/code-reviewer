from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from iot_ops_agent.integrations import remote as sl100_remote
from iot_ops_agent.diagnosis.log_core import LogSnapshot, extract_log_facts


class Sl100RemoteTests(unittest.TestCase):
    def test_extract_log_facts_merges_multiple_snapshots_for_same_service(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="error.log",
                service="deviceShadow",
                line_count=1,
                content='2026-07-09T09:20:00+0800 error websocket read error {"error":"close"}',
            ),
            LogSnapshot(
                path="debug.log",
                service="deviceShadow",
                line_count=1,
                content="2026-07-09T09:21:00+0800 info ok",
            ),
        ])

        service = facts["services"]["deviceShadow"]
        self.assertEqual(service["error_count"], 1)
        self.assertEqual(service["line_count"], 2)
        self.assertEqual(service["paths"], ["error.log", "debug.log"])

    def test_list_remote_logs_includes_known_services(self) -> None:
        logs = sl100_remote.list_remote_logs("gateway")
        names = {item["log"] for item in logs}

        self.assertIn("error", names)
        self.assertIn("stderr", names)
        self.assertTrue(all(item["host"] == "iot-app-a" for item in logs))

    def test_resolve_rejects_unknown_log_name(self) -> None:
        with self.assertRaises(ValueError):
            sl100_remote.resolve_log_ref("gateway", "unknown")

    def test_tail_remote_log_redacts_content(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='2026-07-09 error token=abc {"password":"pw"} 10.1.2.3\n',
            stderr="",
        )

        with patch("iot_ops_agent.integrations.remote.subprocess.run", return_value=completed) as run:
            result = sl100_remote.tail_remote_log("gateway", "error", tail_lines=50)

        self.assertEqual(result["service"], "gateway")
        self.assertIn("<REDACTED_TOKEN>", result["content"])
        self.assertIn("<REDACTED_PASSWORD>", result["content"])
        self.assertIn("<IP>", result["content"])
        command = run.call_args.args[0]
        self.assertEqual(command[5], "iot-app-a")
        self.assertIn("/home/work/service/gateway/log/error.log", command[6])

    def test_search_remote_log_filters_error_like_lines(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="info ok\n2026-07-09 error failed to push\nwarning retry\n",
            stderr="",
        )

        with patch("iot_ops_agent.integrations.remote.subprocess.run", return_value=completed):
            result = sl100_remote.search_remote_log("pushService", "error", limit=10)

        self.assertEqual(len(result), 1)
        self.assertIn("failed", result[0]["content"])

    def test_analyze_remote_logs_uses_existing_rules(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='2026-07-09T09:00:00+0800 error push send error {"error":"timeout"}\n',
            stderr="",
        )

        with patch("iot_ops_agent.integrations.remote.subprocess.run", return_value=completed):
            result = sl100_remote.analyze_remote_logs("pushService", logs=["error"])

        incident_types = {item["type"] for item in result["facts"]["incidents"]}
        self.assertIn("push_failed", incident_types)

    def test_analyze_remote_logs_filters_by_time_window(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=(
                "2026-07-09T09:20:00+0800 error websocket read error {\"error\":\"close\"}\n"
                "2026-07-09T11:20:00+0800 error device mqtt offline subscribe {\"reason\":\"tcp_closed\"}\n"
            ),
            stderr="",
        )

        with patch("iot_ops_agent.integrations.remote.subprocess.run", return_value=completed):
            result = sl100_remote.analyze_remote_logs(
                "deviceShadow",
                logs=["error"],
                from_text="2026-07-09 09:00",
                to_text="2026-07-09 10:00",
            )

        timeline = result["facts"]["timeline"]
        self.assertEqual(len(timeline), 1)
        self.assertIn("09:20", timeline[0]["message"])
        self.assertNotIn("11:20", timeline[0]["message"])


if __name__ == "__main__":
    unittest.main()
