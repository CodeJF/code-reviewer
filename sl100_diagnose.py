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

from sl100_es import analyze_logs as analyze_es_logs
from sl100_incident import build_incident_report, combine_incident_reports, render_incident_report
from sl100_planner import plan_query
from sl100_remote import REMOTE_LOGS, analyze_remote_logs


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
    return facts.get("error_count", 0) == 0 and not facts.get("incidents") and not facts.get("timeline")


def diagnose(query: str, *, size: int = 80, remote_tail_lines: int = 2000, no_remote: bool = False) -> dict[str, Any]:
    plan = plan_query(query)
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
            reports.append(build_incident_report(query=query, analysis=es_analysis, plan={**plan, "services": [service]}))
        except Exception as exc:  # noqa: BLE001 - product report should keep partial progress.
            reports.append(build_incident_report(
                query=query,
                analysis={
                    "facts": {"services": {service: {}}, "source": {"type": "elasticsearch", "service": service}},
                    "diagnosis": {
                        "risk_level": "unknown",
                        "summary": f"ES 查询失败: {exc}",
                        "next_steps": ["检查 sl100-93 SSH、ES 健康状态和索引映射。"],
                    },
                },
                plan={**plan, "services": [service]},
            ))
            continue

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
            reports.append(build_incident_report(query=query, analysis=remote_analysis, plan={**plan, "services": [service]}))
        except Exception as exc:  # noqa: BLE001 - include fallback failure in report.
            reports.append(build_incident_report(
                query=query,
                analysis={
                    "facts": {"services": {service: {}}, "source": {"type": "remote_file", "service": service}},
                    "diagnosis": {
                        "risk_level": "unknown",
                        "summary": f"远程文件日志 fallback 查询失败: {exc}",
                        "next_steps": ["检查 SSH alias、白名单日志路径和服务器权限。"],
                    },
                },
                plan={**plan, "services": [service]},
            ))
    return combine_incident_reports(query, reports, plan=plan)


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
