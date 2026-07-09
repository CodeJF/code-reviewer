from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import sl100_es
from sl100_log_core import redact_text


class Sl100EsTests(unittest.TestCase):
    def test_json_secret_redaction_preserves_json_shape(self) -> None:
        redacted = redact_text('{"token":"abc","password":"pw","secret":"s"}')

        self.assertEqual(
            redacted,
            '{"token":"<REDACTED_TOKEN>","password":"<REDACTED_PASSWORD>","secret":"<REDACTED_SECRET>"}',
        )

    def test_redaction_covers_product_sensitive_values(self) -> None:
        text = (
            '{"token":"abc","secret":"s","uuid":"550e8400-e29b-41d4-a716-446655440000",'
            '"phone":"13800138000","email":"dev@example.com","addr":"10.1.2.3:8080"}'
        )
        redacted = redact_text(text)

        for leaked in ["abc", "550e8400-e29b-41d4-a716-446655440000", "13800138000", "dev@example.com", "10.1.2.3"]:
            self.assertNotIn(leaked, redacted)
        for marker in ["<REDACTED_TOKEN>", "<REDACTED_SECRET>", "<UUID>", "<PHONE>", "<EMAIL>", "<IP>"]:
            self.assertIn(marker, redacted)

    def test_time_window_converts_shanghai_to_utc(self) -> None:
        window = sl100_es.build_time_window(
            from_text="2026-07-09 09:00",
            to_text="2026-07-09 10:00",
        )

        self.assertEqual(window.to_dict()["start_utc"], "2026-07-09T01:00:00.000Z")
        self.assertEqual(window.to_dict()["end_utc"], "2026-07-09T02:00:00.000Z")

    def test_around_time_builds_centered_window(self) -> None:
        window = sl100_es.build_time_window(around_text="2026-07-09 09:20", around_minutes=10)

        self.assertEqual(window.to_dict()["start_utc"], "2026-07-09T01:10:00.000Z")
        self.assertEqual(window.to_dict()["end_utc"], "2026-07-09T01:30:00.000Z")

    def test_index_pattern_for_service_alias(self) -> None:
        window = sl100_es.build_time_window(date_text="2026-07-09")

        self.assertEqual(
            sl100_es.index_pattern_for_service("device-shadow", window=window),
            "api-device-shadow-2026-07-09",
        )

    def test_or_keyword_builds_should_query(self) -> None:
        window = sl100_es.build_time_window(date_text="2026-07-09")
        body = sl100_es.build_search_body(keyword="websocket OR error", window=window)

        keyword_query = body["query"]["bool"]["must"][0]
        self.assertIn("should", keyword_query["bool"])
        self.assertEqual(keyword_query["bool"]["minimum_should_match"], 1)
        self.assertEqual(len(keyword_query["bool"]["should"]), 2)

    def test_search_logs_redacts_hits(self) -> None:
        response = {
            "hits": {
                "total": {"value": 1, "relation": "eq"},
                "hits": [
                    {
                        "_index": "api-device-shadow-2026-07-09",
                        "_id": "abc",
                        "_source": {
                            "@timestamp": "2026-07-09T01:27:26.788Z",
                            "host": {"hostname": "host-1", "ip": ["10.0.0.1"]},
                            "fields": {"log_type": "device-shadow"},
                            "message": "2026-07-09T09:27:21+0800 error websocket read error {\"addr\":\"10.1.2.3:1234\",\"token\":\"abc\"}",
                        },
                    }
                ],
            }
        }

        with patch.object(sl100_es, "_ssh_curl", return_value=json.dumps(response)):
            result = sl100_es.search_logs(
                service="deviceShadow",
                keyword="websocket",
                date_text="2026-07-09",
            )

        self.assertEqual(result["service"], "deviceShadow")
        self.assertIn("<IP>", result["hits"][0]["message"])
        self.assertNotIn("10.1.2.3", result["hits"][0]["message"])
        self.assertIn("<REDACTED_TOKEN>", result["hits"][0]["message"])

    def test_facts_from_es_search_uses_existing_rules(self) -> None:
        search_result = {
            "service": "deviceShadow",
            "index_pattern": "api-device-shadow-2026-07-09",
            "keyword": "websocket",
            "time_window": {},
            "total": {"value": 1, "relation": "eq"},
            "hits": [
                {
                    "message": "2026-07-09T09:27:21+0800 error websocket send error {\"error\":\"broken pipe\"}",
                }
            ],
        }

        facts = sl100_es.facts_from_es_search(search_result)

        self.assertEqual(facts["services"]["deviceShadow"]["error_count"], 1)
        self.assertIn("websocket_failed", {item["type"] for item in facts["incidents"]})


if __name__ == "__main__":
    unittest.main()
