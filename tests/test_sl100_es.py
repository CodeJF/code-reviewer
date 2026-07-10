from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import sl100_es
from sl100_log_core import LogSnapshot, assert_redacted, extract_log_facts, redact_text


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

    def test_redaction_assertion_rejects_unredacted_token_assignment(self) -> None:
        self.assertIn("token_assignment", assert_redacted("token=secret-value"))

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

    def test_missing_daily_index_returns_empty_list_without_crashing(self) -> None:
        response = {
            "error": {"type": "index_not_found_exception", "reason": "no such index"},
            "status": 404,
        }

        with patch.object(sl100_es, "_ssh_curl", return_value=json.dumps(response)):
            indices = sl100_es.list_indices(date_text="2026-07-09", service="gateway")

        self.assertEqual(indices, [])

    def test_search_raises_typed_error_instead_of_returning_false_empty_result(self) -> None:
        response = {
            "error": {"type": "index_not_found_exception", "reason": "no such index"},
            "status": 404,
        }

        with patch.object(sl100_es, "_ssh_curl", return_value=json.dumps(response)):
            with self.assertRaises(sl100_es.ElasticsearchIndexNotFound):
                sl100_es.search_logs(service="gateway", date_text="2026-07-09")

    def test_search_drops_only_unsafe_hits(self) -> None:
        response = {
            "hits": {
                "total": {"value": 2, "relation": "eq"},
                "hits": [
                    {"_index": "api-device-shadow-2026-07-09", "_id": "safe", "_source": {"message": "info safe", "host": {}, "fields": {}}},
                    {"_index": "api-device-shadow-2026-07-09", "_id": "unsafe", "_source": {"message": "info unsafe", "host": {}, "fields": {}}},
                ],
            }
        }
        with patch.object(sl100_es, "_ssh_curl", return_value=json.dumps(response)), \
             patch.object(sl100_es, "assert_redacted", side_effect=[[], ["password_assignment"]]):
            result = sl100_es.search_logs(service="deviceShadow", date_text="2026-07-09")

        self.assertEqual([hit["id"] for hit in result["hits"]], ["safe"])
        self.assertEqual(result["redaction_dropped_count"], 1)
        self.assertEqual(result["source_status"], "partial")

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

    def test_normal_websocket_close_is_not_an_incident(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="es://deviceShadow",
                service="deviceShadow",
                line_count=1,
                content='2026-07-09T09:27:21+0800 error websocket read error {"error":"websocket: close 1000 (normal): Bye"}',
            )
        ])

        self.assertEqual(facts["error_count"], 0)
        self.assertEqual(facts["incidents"], [])
        self.assertEqual(facts["risk_level"], "low")

    def test_debug_line_with_error_payload_is_not_counted_as_error(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="es://deviceShadow",
                service="deviceShadow",
                line_count=1,
                content='2026-07-09T09:27:21+0800\tdebug\t客户端数据读取错误 {"error":"websocket: close 1006 (abnormal closure)"}',
            )
        ])

        self.assertEqual(facts["services"]["deviceShadow"]["level_counts"]["debug"], 1)
        self.assertEqual(facts["error_count"], 0)
        incident = next(item for item in facts["incidents"] if item["type"] == "websocket_failed")
        self.assertEqual(incident["risk_level"], "medium")

    def test_gateway_wrongpass_is_database_connection_failure_at_medium_risk(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="es://gateway",
                service="gateway",
                line_count=1,
                content='2026-06-15T16:28:29.557+0800 error {"error":"WRONGPASS invalid username-password pair"}',
            )
        ])

        self.assertEqual({item["type"] for item in facts["incidents"]}, {"database_connection_failed"})
        self.assertEqual(facts["risk_level"], "medium")

    def test_gateway_payload_type_mismatch_is_classified(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="es://gateway",
                service="gateway",
                line_count=1,
                content='2026-06-15T09:20:27.501+0800 error decoding key payload: cannot decode string into a map[string]interface {}',
            )
        ])

        self.assertEqual({item["type"] for item in facts["incidents"]}, {"payload_type_mismatch"})
        self.assertEqual(facts["risk_level"], "medium")

    def test_single_online_or_offline_event_is_not_flapping(self) -> None:
        facts = extract_log_facts([
            LogSnapshot(
                path="es://deviceShadow",
                service="deviceShadow",
                line_count=1,
                content="2026-07-09T09:20:00+0800 info device mqtt offline subscribe uuid=device-001",
            )
        ])

        self.assertNotIn("device_online_offline_flapping", {item["type"] for item in facts["incidents"]})


if __name__ == "__main__":
    unittest.main()
