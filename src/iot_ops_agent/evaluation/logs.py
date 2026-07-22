"""
M2: Golden log evals for the SL100 log diagnosis tool.

Default mode is local-only and does not call Claude. Use --use-ai when you want
to evaluate model output as well.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from iot_ops_agent.diagnosis.log_core import analyze_paths, extract_log_facts, read_log_file


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate SL100 log diagnosis quality.")
    parser.add_argument("--cases", default="evals/sl100_log_cases.json")
    parser.add_argument("--output", default="artifacts/sl100_eval_results.json")
    parser.add_argument("--use-ai", action="store_true", help="Call Claude for each case.")
    return parser.parse_args()


def run_checks(case: dict, facts: dict, diagnosis: dict | None = None) -> list[dict]:
    expected = case["expected"]
    incidents = facts.get("incidents", [])
    incident_types = {item["type"] for item in incidents}
    services = set(facts.get("services", {}).keys())
    checks = []

    for incident_type in expected.get("incident_types", []):
        passed = incident_type in incident_types
        checks.append({
            "name": f"has_incident:{incident_type}",
            "passed": passed,
            "reason": f"实际 incidents={sorted(incident_types)}",
        })

    allowed_incidents = expected.get("allowed_incident_types")
    if allowed_incidents is not None:
        unexpected = incident_types - set(allowed_incidents)
        checks.append({
            "name": "no_unexpected_incidents",
            "passed": not unexpected,
            "reason": f"允许 incidents={allowed_incidents}, 额外 incidents={sorted(unexpected)}",
        })

    for service in expected.get("services", []):
        passed = service in services or any(service in item.get("related_services", []) for item in incidents)
        checks.append({
            "name": f"has_service:{service}",
            "passed": passed,
            "reason": f"实际 services={sorted(services)}",
        })

    min_errors = expected.get("min_errors")
    if min_errors is not None:
        actual = facts.get("error_count", 0)
        checks.append({
            "name": "min_errors",
            "passed": actual >= min_errors,
            "reason": f"错误数 {actual} >= {min_errors}",
        })

    if diagnosis is not None:
        checks.append({
            "name": "ai_json_has_incidents",
            "passed": isinstance(diagnosis.get("incidents"), list),
            "reason": "Claude 输出包含 incidents 列表",
        })

    return checks


def main() -> int:
    args = parse_args()
    cases = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    results = []
    passed_cases = 0
    passed_checks = 0
    total_checks = 0

    print(f"开始 SL100 日志评测，共 {len(cases)} 个用例\n")

    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        for index, case in enumerate(cases, start=1):
            service_prefix = case["expected"].get("services", ["sl100"])[0]
            log_path = tmpdir / f"{service_prefix}_{case['id']}.log"
            log_path.write_text(case["log"], encoding="utf-8")
            snapshot = read_log_file(str(log_path))
            facts = extract_log_facts([snapshot])
            diagnosis = analyze_paths([str(log_path)], use_ai=True) if args.use_ai else None
            checks = run_checks(case, facts, diagnosis)
            case_passed = all(check["passed"] for check in checks)
            total_checks += len(checks)
            passed_checks += sum(1 for check in checks if check["passed"])
            if case_passed:
                passed_cases += 1

            print(f"[{index}/{len(cases)}] {case['description']}")
            for check in checks:
                status = "PASS" if check["passed"] else "FAIL"
                print(f"  [{status}] {check['name']} - {check['reason']}")
            print()

            results.append({
                "id": case["id"],
                "description": case["description"],
                "status": "pass" if case_passed else "fail",
                "checks": checks,
                "facts_summary": {
                    "risk_level": facts.get("risk_level"),
                    "error_count": facts.get("error_count"),
                    "incidents": [item["type"] for item in facts.get("incidents", [])],
                },
            })

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
