"""Replay locally reviewed real SL100 cases against the live read-only ES logs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from iot_ops_agent.diagnosis.incident import build_incident_report
from iot_ops_agent.diagnosis.log_core import local_diagnosis
from iot_ops_agent.diagnosis.real_cases import LOCAL_CASES_PATH, read_jsonl
from iot_ops_agent.evaluation.review_cases import load_candidate_hit
from iot_ops_agent.integrations.elasticsearch import facts_from_es_search


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate SL100 diagnosis quality on locally reviewed real cases.")
    parser.add_argument("--cases", default=str(LOCAL_CASES_PATH))
    parser.add_argument("--output", default="artifacts/sl100_real_eval_results.json")
    parser.add_argument("--enforce-gates", action="store_true", help="Return non-zero until the personal-use quality gates pass.")
    return parser.parse_args()


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    lookup = case["lookup"]
    candidate = {
        "service": lookup["service"],
        "index": lookup["index"],
        "es_id": lookup["es_id"],
        "around": lookup["around"],
        "around_minutes": lookup.get("around_minutes", 10),
        "keyword": lookup["keyword"],
    }
    expected = case["expected"]
    result: dict[str, Any] = {"id": case["id"], "expected": expected, "checks": []}
    snapshot = case.get("snapshot")
    if snapshot:
        search_result = snapshot
        hit = (snapshot.get("hits") or [None])[0]
        result["source"] = "private_replay"
        retrieved = hit is not None
        result["checks"].append({
            "name": "retrieval",
            "passed": retrieved,
            "reason": "loaded reviewed redacted replay snapshot" if retrieved else "replay snapshot has no evidence",
        })
    else:
        result["source"] = "legacy_live_lookup"
        try:
            hit, search_result = load_candidate_hit(candidate)
        except Exception as exc:  # noqa: BLE001 - legacy real data can expire or become unavailable.
            result["checks"].append({"name": "retrieval", "passed": False, "reason": f"query failed: {exc}"})
            return result
        retrieved = hit is not None
        result["checks"].append({
            "name": "retrieval",
            "passed": retrieved,
            "reason": "matched saved ES id" if retrieved else "saved ES id was not returned",
        })
    if not hit:
        return result

    facts = facts_from_es_search({
        **search_result,
        "hits": search_result.get("hits", [hit]) if snapshot else [hit],
        "total": {"value": 1, "relation": "eq"},
        "raw_returned": 1,
        "redaction_dropped_count": 0,
        "source_status": "ok",
    })
    report = build_incident_report(
        query=case["query"],
        analysis={"facts": facts, "diagnosis": local_diagnosis(facts)},
        plan={"services": [lookup["service"]], "time_window": search_result["time_window"]},
    )
    predicted_types = set(report["facts_summary"]["incidents"])
    expected_types = set(expected.get("incident_types", []))
    result["predicted"] = {
        "types": sorted(predicted_types),
        "risk_level": report["risk_level"],
        "result_status": report["result_status"],
        "redaction_status": report["redaction_status"],
    }
    result["checks"].extend([
        {"name": "services", "passed": set(expected.get("services", [])) <= set(report["services"]), "reason": str(report["services"])},
        {"name": "redaction", "passed": report["redaction_status"] == "passed", "reason": report["redaction_status"]},
    ])
    if expected["verdict"] == "normal":
        result["checks"].extend([
            {"name": "normal_has_no_incident", "passed": not predicted_types, "reason": str(sorted(predicted_types))},
            {"name": "normal_not_high", "passed": report["risk_level"] != "high", "reason": report["risk_level"]},
        ])
    else:
        result["checks"].extend([
            {"name": "incident_types", "passed": expected_types <= predicted_types, "reason": str(sorted(predicted_types))},
            {"name": "risk_level", "passed": report["risk_level"] == expected["risk_level"], "reason": report["risk_level"]},
        ])
    return result


def _metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    type_tp = type_fp = type_fn = 0
    retrieval_ok = 0
    normal_count = incident_count = normal_high = 0
    for result in results:
        expected = result["expected"]
        checks = {check["name"]: check["passed"] for check in result["checks"]}
        retrieval_ok += int(checks.get("retrieval", False))
        predicted = set(result.get("predicted", {}).get("types", []))
        expected_types = set(expected.get("incident_types", []))
        type_tp += len(predicted & expected_types)
        type_fp += len(predicted - expected_types)
        type_fn += len(expected_types - predicted)
        if expected["verdict"] == "normal":
            normal_count += 1
            normal_high += int(result.get("predicted", {}).get("risk_level") == "high")
        else:
            incident_count += 1
    precision = type_tp / (type_tp + type_fp) if type_tp + type_fp else (1.0 if type_fn == 0 else 0.0)
    recall = type_tp / (type_tp + type_fn) if type_tp + type_fn else 1.0
    total = len(results)
    return {
        "case_count": total,
        "incident_count": incident_count,
        "normal_count": normal_count,
        "retrieval_rate": retrieval_ok / total if total else 0.0,
        "type_precision": precision,
        "type_recall": recall,
        "normal_high_false_positives": normal_high,
    }


def _gates(metrics: dict[str, Any]) -> dict[str, bool]:
    return {
        "at_least_20_cases": metrics["case_count"] >= 20,
        "at_least_12_incidents": metrics["incident_count"] >= 12,
        "at_least_8_normals": metrics["normal_count"] >= 8,
        "retrieval_at_least_90_percent": metrics["retrieval_rate"] >= 0.90,
        "type_precision_at_least_85_percent": metrics["type_precision"] >= 0.85,
        "type_recall_at_least_85_percent": metrics["type_recall"] >= 0.85,
        "no_normal_high_false_positive": metrics["normal_high_false_positives"] == 0,
    }


def main() -> int:
    args = parse_args()
    cases = [case for case in read_jsonl(Path(args.cases)) if case.get("expected", {}).get("verdict") in {"incident", "normal"}]
    if not cases:
        print("没有已确认的真实案例。先运行 iot-ops cases collect 和 iot-ops cases review。")
        return 2

    results = [evaluate_case(case) for case in cases]
    metrics = _metrics(results)
    gates = _gates(metrics)
    payload = {"metrics": metrics, "gates": gates, "results": results}
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"真实案例: {metrics['case_count']}，故障: {metrics['incident_count']}，正常: {metrics['normal_count']}")
    print(f"日志证据命中率: {metrics['retrieval_rate']:.0%}")
    print(f"问题类型 precision / recall: {metrics['type_precision']:.0%} / {metrics['type_recall']:.0%}")
    print(f"正常行为被报高风险: {metrics['normal_high_false_positives']}")
    print("交付门槛:")
    for name, passed in gates.items():
        print(f"- {'PASS' if passed else 'PENDING'} {name}")
    print(f"详细结果已保存到: {args.output}")
    return 0 if not args.enforce_gates or all(gates.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
