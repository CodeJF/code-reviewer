"""
Product CLI for SL100 incident diagnosis.

This is the primary CLI surface: give it a natural-language incident, and it
plans ES queries, optionally falls back to remote file logs, and emits one
unified report.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from iot_ops_agent.integrations.elasticsearch import analyze_logs as analyze_es_logs
from iot_ops_agent.integrations.elasticsearch import build_time_window
from iot_ops_agent.diagnosis.incident import build_incident_report, combine_incident_reports, render_incident_report
from iot_ops_agent.diagnosis.log_core import redact_text
from iot_ops_agent.diagnosis.planner import plan_query
from iot_ops_agent.integrations.remote import REMOTE_LOGS, analyze_remote_logs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose a SL100 incident from real logs.")
    parser.add_argument("query", help="Natural-language incident description.")
    parser.add_argument("--json", action="store_true", help="Print unified incident report JSON.")
    parser.add_argument("--output", help="Save the unified incident report JSON to this path.")
    parser.add_argument("--dry-run", action="store_true", help="Print query plan only; do not query ES or SSH logs.")
    parser.add_argument("--size", type=int, default=80, help="Max ES hits per service.")
    parser.add_argument("--remote-tail-lines", type=int, default=2000, help="Remote fallback tail lines.")
    parser.add_argument("--no-remote", action="store_true", help="Disable remote file log fallback.")
    return parser.parse_args()


def _time_kwargs(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "date_text": plan.get("date", ""),
        "from_text": plan.get("from_time", ""),
        "to_text": plan.get("to_time", ""),
        "around_text": plan.get("around", ""),
        "around_minutes": int(plan.get("around_minutes", 10)),
    }


def _needs_fallback(analysis: dict[str, Any]) -> bool:
    facts = analysis.get("facts", {})
    source = facts.get("source") or {}
    return (
        source.get("status") in {"unavailable", "safety_blocked"}
        or (facts.get("error_count", 0) == 0 and not facts.get("incidents") and not facts.get("timeline"))
    )


def _failed_analysis(source_type: str, service: str, error: Exception) -> dict[str, Any]:
    source = {
        "type": source_type,
        "service": service,
        "status": "unavailable",
        "error": redact_text(str(error))[:500],
    }
    return {
        "facts": {"services": {service: {}}, "source": source, "error_count": 0, "timeline": [], "incidents": []},
        "diagnosis": {
            "risk_level": "unknown",
            "summary": f"{source_type} 查询失败: {source['error']}",
            "next_steps": [
                "确认日志数据源可访问后重试。",
                "不要把数据源失败解释为业务没有异常。",
            ],
        },
    }


def _all_day_plan(plan: dict[str, Any]) -> dict[str, Any]:
    date_text = plan["date"]
    window = build_time_window(date_text=date_text)
    return {
        **plan,
        "from_time": "",
        "to_time": "",
        "around": "",
        "time_window": window.to_dict(),
        "time_strategy": "today_fallback",
    }


def _run_attempt(
    query: str,
    plan: dict[str, Any],
    *,
    size: int,
    remote_tail_lines: int,
    no_remote: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    reports = []
    time_kwargs = _time_kwargs(plan)
    for service in plan["chain_services"]:
        try:
            es_analysis = analyze_es_logs(
                service=service,
                keyword=plan["keyword"],
                size=size,
                use_ai=False,
                **time_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - a product report keeps partial progress.
            es_analysis = _failed_analysis("elasticsearch", service, exc)

        reports.append(build_incident_report(query=query, analysis=es_analysis, plan={**plan, "services": [service]}))

        if no_remote or not _needs_fallback(es_analysis) or service not in REMOTE_LOGS:
            continue
        remote_logs = ["error", "debug"] if "debug" in REMOTE_LOGS[service].get("logs", {}) else ["error"]
        try:
            remote_analysis = analyze_remote_logs(
                service,
                logs=remote_logs,
                tail_lines=remote_tail_lines,
                use_ai=False,
                **time_kwargs,
            )
        except Exception as exc:  # noqa: BLE001 - include fallback failure in the report.
            remote_analysis = _failed_analysis("remote_file", service, exc)
        reports.append(build_incident_report(query=query, analysis=remote_analysis, plan={**plan, "services": [service]}))

    return reports, combine_incident_reports(query, reports, plan=plan)


def diagnose(query: str, *, size: int = 80, remote_tail_lines: int = 2000, no_remote: bool = False) -> dict[str, Any]:
    plan = plan_query(query)
    attempt_plans = [("最近 2 小时", plan)]
    if plan.get("time_strategy") != "recent_then_today":
        attempt_plans = [("指定时间范围", plan)]

    reports = []
    query_attempts = []
    final_plan = plan
    for index, (name, attempt_plan) in enumerate(attempt_plans):
        final_plan = attempt_plan
        attempt_reports, attempt_summary = _run_attempt(
            query,
            attempt_plan,
            size=size,
            remote_tail_lines=remote_tail_lines,
            no_remote=no_remote,
        )
        reports.extend(attempt_reports)
        query_attempts.append({
            "name": name,
            "time_window": attempt_plan["time_window"],
            "result_status": attempt_summary["result_status"],
        })
        if attempt_summary["result_status"] != "no_evidence":
            break
        if plan.get("time_strategy") == "recent_then_today" and index == 0:
            attempt_plans.append(("今天全天", _all_day_plan(plan)))

    return combine_incident_reports(query, reports, plan={**final_plan, "query_attempts": query_attempts})


def main() -> int:
    args = parse_args()
    plan = plan_query(args.query)
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0

    report = diagnose(
        args.query,
        size=args.size,
        remote_tail_lines=args.remote_tail_lines,
        no_remote=args.no_remote,
    )
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"排障报告已保存到: {args.output}", file=sys.stderr)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_incident_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
