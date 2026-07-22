from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from iot_ops_agent.evaluation import real_cases as eval_sl100_real_cases
from iot_ops_agent.evaluation import review_cases as sl100_review_cases
from iot_ops_agent.integrations.elasticsearch import SHANGHAI_TZ
from iot_ops_agent.diagnosis.incident_types import INCIDENT_TYPE_KEYS
from iot_ops_agent.diagnosis.log_core import INCIDENT_RULES
from iot_ops_agent.diagnosis.real_cases import incident_dedup_key, incident_signature


class Sl100RealCasesTests(unittest.TestCase):
    def test_collect_candidates_keeps_fingerprint_not_log_message(self) -> None:
        search_result = {
            "hits": [{
                "index": "api-device-shadow-2026-07-09",
                "id": "es-1",
                "timestamp": "2026-07-09T01:20:00Z",
                "message": "2026-07-09 error websocket token=<REDACTED_TOKEN>",
            }]
        }
        with patch.object(sl100_review_cases, "list_indices", return_value=[{"index": "api-device-shadow-2026-07-09"}]), \
             patch.object(sl100_review_cases, "search_logs", return_value=search_result):
            candidates, errors = sl100_review_cases.collect_candidates(
                since_days=1,
                per_service=1,
                now=datetime(2026, 7, 9, 12, 0, tzinfo=SHANGHAI_TZ),
            )

        self.assertFalse(errors)
        self.assertEqual(len(candidates), 4)
        self.assertNotIn("message", candidates[0])
        self.assertIn("fingerprint", candidates[0])
        self.assertIn("signature", candidates[0])
        self.assertNotIn("<REDACTED_TOKEN>", str(candidates[0]))

    def test_collect_candidates_collapses_request_id_only_duplicates(self) -> None:
        search_result = {
            "hits": [
                {
                    "index": "api-gateway-2026-07-09",
                    "id": "es-1",
                    "timestamp": "2026-07-09T01:20:00Z",
                    "message": '2026-07-09 error decoding key payload: cannot decode string into a map message_id="request-one"',
                },
                {
                    "index": "api-gateway-2026-07-09",
                    "id": "es-2",
                    "timestamp": "2026-07-09T01:20:01Z",
                    "message": '2026-07-09 error decoding key payload: cannot decode string into a map message_id="request-two"',
                },
            ]
        }

        with patch.object(
            sl100_review_cases,
            "list_indices",
            side_effect=lambda service="": [{"index": "api-gateway-2026-07-09"}] if service == "gateway" else [],
        ), patch.object(sl100_review_cases, "search_logs", return_value=search_result):
            candidates, errors = sl100_review_cases.collect_candidates(
                since_days=1,
                per_service=2,
                now=datetime(2026, 7, 9, 12, 0, tzinfo=SHANGHAI_TZ),
            )

        self.assertFalse(errors)
        self.assertEqual(len(candidates), 1)

    def test_hydrate_legacy_case_adds_a_private_dedup_signature(self) -> None:
        message = 'error decoding key payload: cannot decode string into a map message_id="request-one"'
        cases = [{
            "id": "real-legacy",
            "lookup": {
                "service": "gateway",
                "index": "api-gateway-2026-07-09",
                "es_id": "es-1",
                "around": "2026-07-09 09:20",
                "around_minutes": 10,
                "keyword": "error",
            },
        }]

        with patch.object(sl100_review_cases, "load_candidate_hit", return_value=({"message": message}, {})):
            keys, errors, changed = sl100_review_cases.hydrate_reviewed_case_dedup_keys(cases)

        signature = incident_signature(message)
        self.assertTrue(changed)
        self.assertFalse(errors)
        self.assertEqual(keys, {incident_dedup_key("gateway", signature)})
        self.assertEqual(cases[0]["lookup"]["signature"], signature)
        self.assertNotIn(message, str(cases))

    def test_review_uses_closed_type_menu_instead_of_free_text(self) -> None:
        candidate = {
            "id": "real-1",
            "query": "gateway 日志异常",
            "service": "gateway",
            "index": "api-gateway-2026-07-09",
            "es_id": "es-1",
            "around": "2026-07-09 09:20",
            "around_minutes": 10,
            "keyword": "error",
            "fingerprint": "fingerprint",
            "signature": "signature",
            "dedup_key": "gateway:signature",
        }
        report = {
            "facts_summary": {"incidents": ["payload_type_mismatch"]},
            "risk_level": "medium",
        }

        with patch.object(sl100_review_cases, "render_incident_report", return_value="test report"), \
             patch("builtins.input", side_effect=["1", "payload error", "11", "", ""]):
            reviewed = sl100_review_cases._review_case(candidate, report)

        assert reviewed is not None
        self.assertEqual(reviewed["expected"]["incident_types"], ["payload_type_mismatch"])
        self.assertEqual(reviewed["expected"]["risk_level"], "medium")
        self.assertEqual(reviewed["review_status"], "confirmed")

    def test_catalog_covers_every_detectable_incident_type(self) -> None:
        self.assertEqual({rule["type"] for rule in INCIDENT_RULES}, INCIDENT_TYPE_KEYS)

    def test_metrics_do_not_claim_precision_when_incident_types_are_all_missed(self) -> None:
        metrics = eval_sl100_real_cases._metrics([
            {
                "expected": {"verdict": "incident", "incident_types": ["payload_type_mismatch"]},
                "checks": [{"name": "retrieval", "passed": True}],
                "predicted": {"types": [], "risk_level": "medium"},
            }
        ])

        self.assertEqual(metrics["type_precision"], 0.0)
        self.assertEqual(metrics["type_recall"], 0.0)


if __name__ == "__main__":
    unittest.main()
