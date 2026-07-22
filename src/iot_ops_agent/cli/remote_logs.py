"""
CLI for read-only SSH access to whitelisted SL100 service log files.

Examples:
  uv run iot-ops remote list
  uv run iot-ops remote tail --service gateway --log error --tail-lines 100
  uv run iot-ops remote search --service pushService --log stderr --keyword panic
  uv run iot-ops remote analyze --service AdminService --logs error,stderr
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from iot_ops_agent.diagnosis.incident import build_incident_report, render_incident_report
from iot_ops_agent.integrations.remote import (
    analyze_remote_logs,
    list_remote_logs,
    search_remote_log,
    tail_remote_log,
)


def _add_time_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--date", dest="date_text", default="", help="Local date in Asia/Shanghai, e.g. 2026-07-09.")
    parser.add_argument("--from", dest="from_text", default="", help="Local start time, e.g. '2026-07-09 09:00'.")
    parser.add_argument("--to", dest="to_text", default="", help="Local end time, e.g. '2026-07-09 10:00'.")
    parser.add_argument("--around", dest="around_text", default="", help="Local center time, e.g. '2026-07-09 09:20'.")
    parser.add_argument("--around-minutes", type=int, default=10, help="Minutes before/after --around.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read whitelisted SL100 remote service logs over SSH.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List configured remote logs.")
    list_parser.add_argument("--service", default="", help="Optional service filter.")

    tail_parser = subparsers.add_parser("tail", help="Tail one remote log file.")
    tail_parser.add_argument("--service", required=True)
    tail_parser.add_argument("--log", default="error")
    tail_parser.add_argument("--tail-lines", type=int, default=300)
    tail_parser.add_argument("--json", action="store_true")

    search_parser = subparsers.add_parser("search", help="Search one tailed remote log file locally.")
    search_parser.add_argument("--service", required=True)
    search_parser.add_argument("--log", default="error")
    search_parser.add_argument("--keyword", default="", help="Empty means error-like lines.")
    search_parser.add_argument("--tail-lines", type=int, default=2000)
    search_parser.add_argument("--limit", type=int, default=80)
    _add_time_args(search_parser)

    analyze_parser = subparsers.add_parser("analyze", help="Analyze remote file logs with local rules by default.")
    analyze_parser.add_argument("--service", required=True)
    analyze_parser.add_argument("--logs", default="error", help="Comma-separated log names, e.g. error,stderr.")
    analyze_parser.add_argument("--tail-lines", type=int, default=800)
    analyze_parser.add_argument("--use-ai", action="store_true")
    analyze_parser.add_argument("--json", action="store_true")
    analyze_parser.add_argument("--output", help="Optional path to save unified incident report JSON.")
    _add_time_args(analyze_parser)

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.command == "list":
        print(json.dumps(list_remote_logs(args.service), ensure_ascii=False, indent=2))
        return 0

    if args.command == "tail":
        result = tail_remote_log(args.service, args.log, args.tail_lines)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(result["content"])
        return 0

    if args.command == "search":
        result = search_remote_log(
            args.service,
            args.log,
            args.keyword,
            args.tail_lines,
            args.limit,
            date_text=args.date_text,
            from_text=args.from_text,
            to_text=args.to_text,
            around_text=args.around_text,
            around_minutes=args.around_minutes,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "analyze":
        logs = [item.strip() for item in args.logs.split(",") if item.strip()]
        result = analyze_remote_logs(
            args.service,
            logs=logs,
            tail_lines=args.tail_lines,
            use_ai=args.use_ai,
            date_text=args.date_text,
            from_text=args.from_text,
            to_text=args.to_text,
            around_text=args.around_text,
            around_minutes=args.around_minutes,
        )
        report = build_incident_report(
            query=f"{args.service} {args.logs}",
            analysis=result,
            plan={
                "services": [args.service],
                "date": args.date_text,
                "from_time": args.from_text,
                "to_time": args.to_text,
                "around": args.around_text,
            },
        )
        if args.output:
            Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"排障报告已保存到: {args.output}", file=sys.stderr)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(render_incident_report(report))
        return 0

    raise AssertionError(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
