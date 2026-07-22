"""
Product-level evals for the SL100 diagnosis tool.

These cases verify behavior that matters for deliverability rather than model
quality: planning, report shape, safe fallback, and no-hit handling.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from iot_ops_agent.integrations import remote as sl100_remote
from iot_ops_agent.integrations.elasticsearch import SHANGHAI_TZ, build_search_body, build_time_window
from iot_ops_agent.diagnosis.incident import build_incident_report
from iot_ops_agent.diagnosis.log_core import LogSnapshot, extract_log_facts
from iot_ops_agent.diagnosis.planner import plan_query


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate SL100 product behavior.")
    parser.add_argument("--cases", default="evals/sl100_product_cases.json")
    parser.add_argument("--output", default="artifacts/sl100_product_eval_results.json")
    return parser.parse_args()


def _pass(name: str, condition: bool, reason: str) -> dict:
    return {"name": name, "passed": bool(condition), "reason": reason}


def run_case(case: dict) -> list[dict]:
    checks = []
    expected = case["expected"]
    case_type = case["type"]

    if case_type == "planner":
        plan = plan_query(case["query"], now=datetime(2026, 7, 9, 12, 0, tzinfo=SHANGHAI_TZ))
        checks.append(_pass("primary_service", plan["primary_service"] == expected["primary_service"], str(plan)))
        checks.append(_pass("from_time", plan["from_time"] == expected["from_time"], str(plan)))
        checks.append(_pass("to_time", plan["to_time"] == expected["to_time"], str(plan)))
        checks.append(_pass("keyword_contains", expected["keyword_contains"] in plan["keyword"], plan["keyword"]))
        return checks

    if case_type == "or_keyword":
        body = build_search_body(keyword=case["keyword"], window=build_time_window(date_text="2026-07-09"))
        keyword_query = body["query"]["bool"]["must"][0]["bool"]
        checks.append(_pass("minimum_should_match", keyword_query["minimum_should_match"] == expected["minimum_should_match"], str(keyword_query)))
        checks.append(_pass("should_count", len(keyword_query["should"]) == expected["should_count"], str(keyword_query)))
        return checks

    if case_type == "empty_report":
        report = build_incident_report(
            query=case["query"],
            analysis={
                "facts": {"source": {"type": "elasticsearch"}, "services": {"gateway": {}}, "error_count": 0, "timeline": []},
                "diagnosis": {"risk_level": "low", "summary": "empty"},
            },
        )
        checks.append(_pass("root_cause_contains", expected["root_cause_contains"] in report["root_cause"], report["root_cause"]))
        return checks

    if case_type == "required_fields":
        facts = extract_log_facts([
            LogSnapshot(
                path="elasticsearch://api-device-shadow-2026-07-09",
                service="deviceShadow",
                line_count=1,
                content='2026-07-09T09:20:00+0800 error websocket read error {"error":"close"}',
            )
        ])
        report = build_incident_report(
            query=case["query"],
            analysis={"facts": facts, "diagnosis": {"risk_level": "high", "summary": "hit", "incidents": []}},
        )
        for field in expected["fields"]:
            checks.append(_pass(f"has_field:{field}", field in report, sorted(report)))
        return checks

    if case_type == "remote_time_window":
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
        text = json.dumps(result, ensure_ascii=False)
        checks.append(_pass("included", expected["included"] in text, text))
        checks.append(_pass("excluded", expected["excluded"] not in text, text))
        return checks

    raise ValueError(f"unknown case type: {case_type}")


def main() -> int:
    args = parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    results = []
    passed_cases = 0
    passed_checks = 0
    total_checks = 0

    print(f"开始 SL100 产品级评测，共 {len(cases)} 个用例\n")
    for index, case in enumerate(cases, start=1):
        checks = run_case(case)
        total_checks += len(checks)
        passed_checks += sum(1 for check in checks if check["passed"])
        case_passed = all(check["passed"] for check in checks)
        if case_passed:
            passed_cases += 1

        print(f"[{index}/{len(cases)}] {case['id']}")
        for check in checks:
            status = "PASS" if check["passed"] else "FAIL"
            print(f"  [{status}] {check['name']} - {check['reason']}")
        print()
        results.append({"id": case["id"], "status": "pass" if case_passed else "fail", "checks": checks})

    print("=" * 60)
    print(f"用例通过率: {passed_cases}/{len(cases)} ({passed_cases / len(cases) * 100:.0f}%)")
    print(f"检查通过率: {passed_checks}/{total_checks} ({passed_checks / total_checks * 100:.0f}%)")
    print("=" * 60)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存到: {args.output}")
    return 0 if passed_cases == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
